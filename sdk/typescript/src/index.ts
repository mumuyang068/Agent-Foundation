import { FOUNDATION_OPERATIONS, type FoundationOperationId } from "./routes.js";

export { FOUNDATION_OPERATIONS, type FoundationOperationId } from "./routes.js";
export type DomainName = "access" | "memory" | "evidence" | "graph" | "context" | "jobs" | "applications" | "management";
export type JsonObject = Record<string, unknown>;

export interface FoundationClientOptions {
  origin: string;
  token?: string | (() => string | Promise<string>);
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
}

export interface CallOptions {
  path?: Record<string, string | number>;
  query?: Record<string, string | number | boolean | undefined>;
  payload?: unknown;
}

export class FoundationClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly requestId: string | undefined;

  constructor(input: { code: string; status: number; message: string; retryable: boolean; requestId?: string }) {
    super(input.message);
    this.name = "FoundationClientError";
    this.code = input.code;
    this.status = input.status;
    this.retryable = input.retryable;
    this.requestId = input.requestId;
  }
}

export class DomainEndpoint {
  constructor(protected readonly client: FoundationClient, readonly domain: DomainName) {}

  get operations(): string[] {
    const prefix = `${this.domain}.`;
    return Object.keys(FOUNDATION_OPERATIONS)
      .filter((operation) => operation.startsWith(prefix))
      .map((operation) => operation.slice(prefix.length));
  }

  call<T>(operation: string, options: CallOptions = {}): Promise<T> {
    return this.client.call(`${this.domain}.${operation}` as FoundationOperationId, options);
  }
}

export class AccessEndpoint extends DomainEndpoint {
  putGrant<T>(payload: JsonObject): Promise<T> { return this.call("put_grant", { payload }); }
  listGrants<T>(): Promise<T> { return this.call("list_grants"); }
  getGrant<T>(grantId: string): Promise<T> { return this.call("get_grant", { path: { grant_id: grantId } }); }
  issueSession<T>(payload: JsonObject): Promise<T> { return this.call("issue_session", { payload }); }
  getSession<T>(sessionId: string): Promise<T> { return this.call("get_session", { path: { session_id: sessionId } }); }
  revokeSession<T>(sessionId: string): Promise<T> { return this.call("revoke_session", { path: { session_id: sessionId } }); }
  putDelegation<T>(payload: JsonObject): Promise<T> { return this.call("put_delegation", { payload }); }
  getDelegation<T>(delegationId: string): Promise<T> { return this.call("get_delegation", { path: { delegation_id: delegationId } }); }
  revokeDelegation<T>(delegationId: string): Promise<T> { return this.call("revoke_delegation", { path: { delegation_id: delegationId } }); }
  audit<T>(): Promise<T> { return this.call("audit"); }
  authorize<T>(payload: JsonObject): Promise<T> { return this.call("authorize", { payload }); }
  revokeGrant<T>(grantId: string): Promise<T> { return this.call("revoke_grant", { path: { grant_id: grantId } }); }
}

export class MemoryEndpoint extends DomainEndpoint {
  propose<T>(payload: JsonObject): Promise<T> { return this.call("propose", { payload }); }
  get<T>(memoryId: string): Promise<T> { return this.call("get", { path: { memory_id: memoryId } }); }
  review<T>(memoryId: string, payload: JsonObject): Promise<T> { return this.call("review", { path: { memory_id: memoryId }, payload }); }
  correct<T>(memoryId: string, payload: JsonObject): Promise<T> { return this.call("correct", { path: { memory_id: memoryId }, payload }); }
  feedback<T>(memoryId: string, payload: JsonObject): Promise<T> { return this.call("feedback", { path: { memory_id: memoryId }, payload }); }
  recall<T>(payload: JsonObject): Promise<T> { return this.call("recall", { payload }); }
}

export class EvidenceEndpoint extends DomainEndpoint {
  admit<T>(payload: JsonObject): Promise<T> { return this.call("admit", { payload }); }
  get<T>(revisionId: string): Promise<T> { return this.call("get_revision", { path: { revision_id: revisionId } }); }
  search<T>(payload: JsonObject): Promise<T> { return this.call("search", { payload }); }
  revoke<T>(revisionId: string, operationId: string): Promise<T> {
    return this.call("revoke", { path: { revision_id: revisionId }, query: { operation_id: operationId } });
  }
}

export class GraphEndpoint extends DomainEndpoint {
  propose<T>(payload: JsonObject): Promise<T> { return this.call("propose", { payload }); }
  review<T>(proposalId: string, payload: JsonObject): Promise<T> { return this.call("review", { path: { proposal_id: proposalId }, payload }); }
  apply<T>(proposalId: string, payload: JsonObject): Promise<T> { return this.call("apply", { path: { proposal_id: proposalId }, payload }); }
  query<T>(payload: JsonObject): Promise<T> { return this.call("query", { payload }); }
  queryTemporal<T>(payload: JsonObject): Promise<T> { return this.call("query_temporal", { payload }); }
  proposeTemporalRelation<T>(payload: JsonObject): Promise<T> { return this.call("propose_temporal_relation", { payload }); }
  proposeTemporalInvalidation<T>(edgeId: string, payload: JsonObject): Promise<T> {
    return this.call("propose_temporal_invalidation", { path: { edge_id: edgeId }, payload });
  }
  proposeTemporalSupersession<T>(edgeId: string, payload: JsonObject): Promise<T> {
    return this.call("propose_temporal_supersession", { path: { edge_id: edgeId }, payload });
  }
  getNode<T>(nodeId: string): Promise<T> { return this.call("get_node", { path: { node_id: nodeId } }); }
  getEdgeRevision<T>(edgeId: string, revision: number): Promise<T> { return this.call("get_edge_revision", { path: { edge_id: edgeId, revision } }); }
  getSchema<T>(schemaId: string, version: number): Promise<T> { return this.call("get_schema", { path: { schema_id: schemaId, version } }); }
  listSchemas<T>(schemaId?: string, limit = 8): Promise<T> { return this.call("list_schemas", { query: { schema_id: schemaId, limit } }); }
}

export class ContextEndpoint extends DomainEndpoint {
  assemble<T>(payload: JsonObject): Promise<T> { return this.call("assemble", { payload }); }
  snapshot<T>(payload: JsonObject): Promise<T> { return this.call("snapshot", { payload }); }
  replay<T>(snapshotId: string): Promise<T> { return this.call("replay", { path: { snapshot_id: snapshotId } }); }
}

export class JobsEndpoint extends DomainEndpoint {
  plan<T>(payload: JsonObject): Promise<T> { return this.call("plan", { payload }); }
  authorize<T>(jobId: string, payload: JsonObject): Promise<T> { return this.call("authorize", { path: { job_id: jobId }, payload }); }
  runNext<T>(jobId: string, payload: JsonObject): Promise<T> { return this.call("run_next", { path: { job_id: jobId }, payload }); }
  get<T>(jobId: string): Promise<T> { return this.call("get", { path: { job_id: jobId } }); }
  cancel<T>(jobId: string, payload: JsonObject): Promise<T> { return this.call("cancel", { path: { job_id: jobId }, payload }); }
}

export class ApplicationsEndpoint extends DomainEndpoint {
  prepare<T>(payload: JsonObject): Promise<T> { return this.call("prepare", { payload }); }
  get<T>(turnId: string): Promise<T> { return this.call("get", { path: { turn_id: turnId } }); }
  observe<T>(turnId: string, payload: JsonObject): Promise<T> { return this.call("observe", { path: { turn_id: turnId }, payload }); }
  outcome<T>(turnId: string, payload: JsonObject): Promise<T> { return this.call("outcome", { path: { turn_id: turnId }, payload }); }
}

export class ManagementEndpoint extends DomainEndpoint {
  diagnostics<T>(): Promise<T> { return this.call("diagnostics"); }
  stageRelease<T>(payload: JsonObject): Promise<T> { return this.call("stage_release", { payload }); }
  approveRelease<T>(releaseId: string, payload: JsonObject): Promise<T> { return this.call("approve_release", { path: { release_id: releaseId }, payload }); }
  activateRelease<T>(releaseId: string, payload: JsonObject): Promise<T> { return this.call("activate_release", { path: { release_id: releaseId }, payload }); }
  activeRelease<T>(kind: string, artifactId: string): Promise<T> {
    return this.call("active_release", { query: { kind, artifact_id: artifactId } });
  }
  listReleases<T>(kind?: string, status?: string, limit = 20): Promise<T> { return this.call("list_releases", { query: { kind, status, limit } }); }
  listIndexes<T>(domain?: string, status?: string, limit = 20): Promise<T> { return this.call("list_indexes", { query: { domain, status, limit } }); }
  getEvaluation<T>(evaluationId: string): Promise<T> { return this.call("get_evaluation", { path: { evaluation_id: evaluationId } }); }
  listEvaluations<T>(subjectRef?: string, limit = 20): Promise<T> { return this.call("list_evaluations", { query: { subject_ref: subjectRef, limit } }); }
  listTransfers<T>(kind?: string, status?: string, limit = 20): Promise<T> { return this.call("list_transfers", { query: { kind, status, limit } }); }
  certifyExtractor<T>(releaseId: string, payload: JsonObject): Promise<T> { return this.call("certify_extractor", { path: { release_id: releaseId }, payload }); }
  getExtractorCertificate<T>(certificateId: string): Promise<T> { return this.call("get_extractor_certificate", { path: { certificate_id: certificateId } }); }
  revokeExtractorCertificate<T>(certificateId: string, payload: JsonObject): Promise<T> { return this.call("revoke_extractor_certificate", { path: { certificate_id: certificateId }, payload }); }
  executeExtractor<T>(releaseId: string, payload: JsonObject): Promise<T> { return this.call("execute_extractor", { path: { release_id: releaseId }, payload }); }
  getExtractorExecution<T>(eventId: string): Promise<T> { return this.call("get_extractor_execution", { path: { event_id: eventId } }); }
}

export class FoundationClient {
  readonly origin: string;
  readonly access: AccessEndpoint;
  readonly memory: MemoryEndpoint;
  readonly evidence: EvidenceEndpoint;
  readonly graph: GraphEndpoint;
  readonly context: ContextEndpoint;
  readonly jobs: JobsEndpoint;
  readonly applications: ApplicationsEndpoint;
  readonly management: ManagementEndpoint;
  private readonly token: FoundationClientOptions["token"];
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(options: FoundationClientOptions) {
    const parsed = new URL(options.origin);
    if (!/^https?:$/.test(parsed.protocol) || parsed.pathname !== "/" || parsed.search || parsed.hash) {
      throw new TypeError("Foundation origin must be an explicit HTTP(S) origin");
    }
    if (parsed.username || parsed.password) throw new TypeError("Foundation origin must not contain credentials");
    this.origin = parsed.origin;
    this.token = options.token;
    this.timeoutMs = options.timeoutMs ?? 20_000;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    this.access = new AccessEndpoint(this, "access");
    this.memory = new MemoryEndpoint(this, "memory");
    this.evidence = new EvidenceEndpoint(this, "evidence");
    this.graph = new GraphEndpoint(this, "graph");
    this.context = new ContextEndpoint(this, "context");
    this.jobs = new JobsEndpoint(this, "jobs");
    this.applications = new ApplicationsEndpoint(this, "applications");
    this.management = new ManagementEndpoint(this, "management");
  }

  get operations(): FoundationOperationId[] { return Object.keys(FOUNDATION_OPERATIONS) as FoundationOperationId[]; }

  call<T>(operationId: FoundationOperationId, options: CallOptions = {}): Promise<T> {
    const operation = FOUNDATION_OPERATIONS[operationId];
    if (!operation) throw new TypeError(`Unknown Foundation operation: ${operationId}`);
    const required = [...operation.path.matchAll(/\{([^}]+)\}/g)].map((match) => match[1] as string);
    const supplied = Object.keys(options.path ?? {});
    if (required.length !== supplied.length || required.some((name) => !supplied.includes(name))) {
      throw new TypeError(`Operation ${operationId} needs path parameters ${required.sort().join(",")}`);
    }
    let path: string = operation.path;
    for (const name of required) {
      const value = options.path?.[name];
      if (value === undefined || String(value) === "") throw new TypeError(`Path parameter ${name} cannot be empty`);
      path = path.replace(`{${name}}`, encodeURIComponent(String(value)));
    }
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(options.query ?? {})) {
      if (value !== undefined) query.set(name, String(value));
    }
    const suffix = query.size ? `${path}?${query}` : path;
    return this.request(operation.method, suffix, options.payload);
  }

  async request<T>(method: "GET" | "POST" | "PUT" | "DELETE" | "PATCH", path: string, body?: unknown): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const token = typeof this.token === "function" ? await this.token() : this.token;
      const headers: Record<string, string> = { accept: "application/json" };
      if (body !== undefined) headers["content-type"] = "application/json";
      if (token) headers.authorization = `Bearer ${token}`;
      const init: RequestInit = { method, headers, signal: controller.signal };
      if (body !== undefined) init.body = JSON.stringify(body);
      const response = await this.fetchImpl(`${this.origin}/api/foundation/v1${path}`, init);
      const requestId = response.headers.get("x-request-id") ?? undefined;
      const payload: unknown = await response.json();
      if (!response.ok) {
        const error = isObject(payload) && isObject(payload.error) ? payload.error : {};
        throw new FoundationClientError({
          code: typeof error.code === "string" ? error.code : "http_error",
          status: response.status,
          message: typeof error.message === "string" ? error.message : `Foundation request failed with HTTP ${response.status}`,
          retryable: typeof error.retryable === "boolean" ? error.retryable : response.status >= 500,
          ...(requestId ? { requestId } : {}),
        });
      }
      return payload as T;
    } finally {
      clearTimeout(timer);
    }
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
