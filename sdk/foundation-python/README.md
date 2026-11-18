# Agent Foundation Python client

`agent-foundation-client` is the transport-only client for `/api/foundation/v1`.
It does not install or import the Agent Foundation service implementation.

```python
from agent_foundation_client import FoundationClient

with FoundationClient("http://127.0.0.1:8080") as foundation:
    descriptor = foundation.management.host()
```
