# Agent Foundation TypeScript client

`@agent-foundation/client` is the transport-only client for `/api/foundation/v1`.
It contains no service runtime, database adapter, worker, migration, product rule,
or legacy Registration fallback.

```ts
import { FoundationClient } from "@agent-foundation/client";

const foundation = new FoundationClient({ origin: "http://127.0.0.1:8080" });
const descriptor = await foundation.management.host();
```
