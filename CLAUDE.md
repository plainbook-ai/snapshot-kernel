# Snapshot Python Checkpointing Kernel Project

I want to develop a checkpoining Python kernel, mainly geared at executing Jupyter Python notebooks. 

## Kernel Basics

The kernel will store states, where a state is a snapshot of the execution environment, including among others: 

* Timestamp when the state was created
* Variables
* Imported modules

States are immutable.  They are not modified; however, new states can be created from existing states by executing code in that state. 

The kernel should be able to store multiple states, in the form of a dictionary mapping state names to their content. 
The main methods that the kernel should implement are: 

* `execute(code: str, exec_id: str, state_name: str, new_state_name: Optional[str] = None): dict`: Executes the given code in the specified state. `exec_id` is a unique ID for this execution.  If `new_state_name` is provided, the resulting state after execution will be stored under that name. Otherwise, the resulting state will be stored under a new randomly generated unique name. The output of the execution should be a dictionary including at least: 
    * `output`: The output of the execution, if any, as a list in the same format as the output of a Jupyter notebook cell execution.
    * `state_name`: The name of the state after execution.
    * `error`: Any error that occurred during execution, if applicable.
    * `accessed_symbols`: The list of top-level variable names that were present in the state before execution and were read during the execution (the cell's dependencies on pre-existing variables), e.g. `["df", "x"]`. Names the cell itself defines are not included. On interpreters where read-tracking cannot be relied upon, this conservatively lists all pre-existing symbols. `multistate_execute` returns this same `accessed_symbols` key.
    * `modified_symbols`: The list of top-level variable names assigned during execution (the cell's writes), e.g. `["y"]`. Best-effort (dunder names and the injected `display` helper are excluded).
    * `deleted_symbols`: The list of top-level variable names removed with `del` during execution.

* `get_state(state_name: str) -> dict`: Retrieves the state associated with the given name. The state should include all variables and imported modules at that point in execution.

* `get_symbol_hashes(state_name: str, symbols: List[str], hash_algo: Optional[str] = None) -> dict`: Returns a dict mapping each requested symbol to a stable content hash of its value in the given state, used to detect whether symbols changed between states. `hash_algo` selects the strategy; currently only `"full"` is supported (the default when `None`), which hashes the full value (pickled bytes, SHA-256). Symbols absent from the state map to `null`; returns `None` (HTTP 404) if the state does not exist. Exposed over HTTP as `POST /symbol_hashes` with body `{state_name, symbols, hash_algo?}`, returning `{"hashes": {...}}`.

* `get_alias_groups(state_name: str) -> dict`: Returns `{"groups": [[names...], ...], "fingerprints": [hex, ...]}` — the alias groups of the state (maximal sets of top-level variables that share a mutable object, directly or nested) and, for each group, a fingerprint that hashes the group's members together (so it reflects both their values and the sharing among them). Every non-dunder user variable appears in exactly one group (singletons included). Returns `None` (HTTP 404) if the state does not exist. Exposed as `POST /alias_groups` with body `{state_name}`.

* `rebuild_state(input_state: str, source_state: str, source_vars: List[str], input_vars: List[str], new_state_name: Optional[str] = None) -> dict`: Reconstructs a successor state *without executing code*: the variables in `source_vars` are copied from `source_state`, the variables in `input_vars` from `input_state`, each side under its own shared deepcopy memo (so aliasing within each side is preserved). The caller must guarantee the two sides are alias-disjoint. Returns `{state_name, groups, fingerprints}` for the new state, or `None` (HTTP 404) if either state is missing. Exposed as `POST /rebuild_state` with body `{input_state, source_state, source_vars, input_vars, new_state_name?}`.

* `list_states() -> List[str]`: Returns a list of all state names currently stored in the kernel.

* `delete_state(state_name: str)`: Deletes the state associated with the given name from the kernel.

* `reset()`: Resets the kernel by clearing all stored states and returning to an initial empty state.

* `interrupt(exec_id: str)`: Interrupts the execution associated with the given `exec_id`. This should stop the execution of the code and return an appropriate response indicating that the execution was interrupted.

## Implementation Details

The kernel should be written in Python.  The Python code should be: 
* Indented with 4-space indentation
* Type hints not necessary
* Docstrings should be included. 

Try to use the simplest code, using as few external (imported) modules as possible. 

State snapshots preserve intra-state aliasing: the namespace is deep-copied with a single shared `copy.deepcopy` memo, so two variables that reference the same object (directly or nested) remain aliased within a state — matching standard Python semantics — while distinct states stay fully independent. Modules are stored by reference.

The kernel execution itself should be separate from the server code described below, so at least a couple of Python files are necessary, a `main.py` for the bottle server, and a `kernel.py` for the kernel implementation. 

Try to keep the implementation organized in a clean class structure. 

You should format this as a Python package, with a `snapshot_kernel` directory containing the `__init__.py`, `kernel.py`, and `main.py` files, and a `pyproject.toml` file for installing the package at top level. 

## Output Format

The output generated by the kernel should be in a format that is compatible with Jupyter notebook cell outputs.  This means that the output should be a list of dictionaries, where each dictionary represents an output item and has at least the following keys:

* `output_type`: A string indicating the type of output (e.g., "stream", "display_data", "execute_result", "error", etc.).
* `data`: The actual output data, which can be in various formats depending on the output type (e.g., text, HTML, images, etc.).
* `metadata`: Any additional metadata associated with the output, such as MIME types, execution count, etc.

In particular, it is important to be able to capture both standard output and standard error, and also: 
* Figures / plots generated by the code, such as those generated by matplotlib, should be captured and included in the output in a format that can be rendered in a Jupyter notebook (e.g., as base64-encoded PNG images).
* Rich outputs, such as those generated by libraries like pandas (e.g., DataFrames), should also be captured and included in the output in a format that can be rendered in a Jupyter notebook (e.g., as HTML tables).

The test cases should include tests for these formats. 

## Communication with the Kernel

Communication with the kernel will occur over a rest API using HTTP requests, and the bottle.py web server, using cheroot as the WSGI server to enable multi-threading.  The multithreading is necessary to allow for interrupting long-running executions.  Note that we are also not ruling out the possibility of computing multiple states in parallel, generating multiple output states from the same input state. 

The bottle server should be launched with a command of the form: 

```bash
python -m bottle --bind <IP_ADDRESS>:8080 --token=<SECRET_TOKEN> main.py
```

where `kernel_server.py` is the file containing the implementation of the kernel and the bottle server, and SECRET_TOKEN is a token that should be specified as a URL parameter in every request for authentication. The server should listen for incoming HTTP requests and route them to the appropriate methods of the kernel based on the request path and method (e.g., POST for executing code, GET for retrieving states, etc.).
