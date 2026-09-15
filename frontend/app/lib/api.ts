export type IdentityMode = "customer" | "approver";

export type Identity = {
  tenant_id: string;
  actor_id: string;
  role: "CUSTOMER" | "APPROVER" | "STAFF" | "ADMIN";
};

export type TicketSummary = {
  id: string;
  thread_id: string;
  status: string;
  processing_result: string | null;
  subject: string;
  category: string | null;
  priority: string | null;
  risk_level: string | null;
  order_reference: string | null;
  created_at: string;
  updated_at: string;
};

export type Message = {
  id: string;
  run_id: string | null;
  role: "CUSTOMER" | "AGENT" | "STAFF";
  content: string;
  citations: Array<{ source_id: string; chunk_id?: string; title: string; excerpt?: string; policy_version?: string; effective_at?: string }>;
  created_at: string;
};

export type Approval = {
  id: string;
  ticket_id?: string;
  run_id?: string;
  subject?: string;
  order_reference?: string | null;
  action_type: string;
  action_payload: Record<string, unknown>;
  status: string;
  requested_at: string;
  decided_by?: string | null;
  decision_reason?: string | null;
  decided_at?: string | null;
  executed_at?: string | null;
};

export type TicketDetail = TicketSummary & {
  resolution_summary: string | null;
  order: {
    order_reference: string;
    payment_status: string;
    fulfillment_status: string;
    paid_amount: string;
    refundable_amount: string;
    currency: string;
    carrier: string | null;
    tracking_number_masked: string | null;
    estimated_delivery_at: string | null;
  } | null;
  messages: Message[];
  pending_approval: Approval | null;
};

export type RunEvent = {
  id: string;
  ticket_id: string | null;
  run_id: string | null;
  approval_id: string | null;
  actor_type: string;
  event_type: string;
  node_name: string | null;
  tool_name: string | null;
  outcome: string;
  details: Record<string, unknown>;
  occurred_at: string;
};

export type Dashboard = {
  tenant_id: string;
  order_count: number;
  customer_count: number;
  ticket_count: number;
  open_ticket_count: number;
  refund_ticket_count: number;
  contact_rate: string;
  avg_resolution_minutes: string | null;
  datasets: Array<{
    dataset_id: string;
    loaded_at: string;
    evidence_source: string;
    row_counts: Record<string, number>;
    total_rows: number;
  }>;
  daily_volume: Array<{
    day: string;
    created_count: number;
    refund_count: number;
    resolved_count: number;
    failed_count: number;
  }>;
  refund_funnel: Array<{
    status: string;
    approval_count: number;
    requested_amount: string;
  }>;
};

const API_BASE = import.meta.env.VITE_TICKETPILOT_API_BASE ?? "/api";
const TOKENS: Record<IdentityMode, string> = {
  customer: import.meta.env.VITE_TICKETPILOT_CUSTOMER_TOKEN ?? "demo-customer-token",
  approver: import.meta.env.VITE_TICKETPILOT_APPROVER_TOKEN ?? "demo-approver-token",
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(mode: IdentityMode, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${TOKENS[mode]}`,
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const error = payload?.error;
    throw new ApiError(
      error?.message ?? `Request failed with HTTP ${response.status}`,
      error?.code ?? "HTTP_ERROR",
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

export const api = {
  identity: (mode: IdentityMode) => request<Identity>(mode, "/v1/me"),
  dashboard: (mode: IdentityMode) => request<Dashboard>(mode, "/v1/dashboard/summary"),
  tickets: (mode: IdentityMode) =>
    request<{ items: TicketSummary[] }>(mode, "/v1/tickets?limit=60"),
  ticket: (mode: IdentityMode, id: string) =>
    request<TicketDetail>(mode, `/v1/tickets/${id}`),
  approvals: () => request<{ items: Approval[] }>("approver", "/v1/approvals?limit=60"),
  runEvents: (mode: IdentityMode, runId: string) =>
    request<{ run_id: string; events: RunEvent[] }>(mode, `/v1/runs/${runId}/events`),
  createTicket: (
    payload: { subject: string; message: string; order_reference?: string },
    idempotencyKey: string,
  ) =>
    request<{ ticket: TicketSummary; run_id: string }>("customer", "/v1/tickets", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
  addMessage: (ticketId: string, message: string, idempotencyKey: string) =>
    request<{ ticket: TicketSummary; run_id: string }>(
      "customer",
      `/v1/tickets/${ticketId}/messages`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ message }),
      },
    ),
  decideApproval: (approvalId: string, decision: "APPROVE" | "REJECT", reason: string) =>
    request<{ ticket: TicketSummary; run_id: string }>(
      "approver",
      `/v1/approvals/${approvalId}:decide`,
      {
        method: "POST",
        body: JSON.stringify({ decision, reason }),
      },
    ),
};

export const compactId = (value: string) => `${value.slice(0, 7)}…${value.slice(-5)}`;
export const formatNumber = (value: number) => new Intl.NumberFormat("zh-CN").format(value);
export const formatTime = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
