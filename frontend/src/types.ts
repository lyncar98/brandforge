export interface AssetRecord {
  key: string;
  asset_id: string;
  group: string;
  label: string;
  status: "completed" | "failed" | "needs_attention" | "pending";
  model: string;
  output_path: string | null;
  failure_code: string | null;
  failure_reason: string | null;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  updated_at: string;
}

export interface StateResponse {
  records: AssetRecord[];
  cost: number;
  counts: Record<string, number>;
}

export interface PlanAction {
  asset_id: string;
  kind: string;
  group: string;
  label: string;
  action: "create" | "update" | "skip" | "destroy";
  model: string;
  detail: string;
  symbol: string;
}

export type LogEvent =
  | { type: "log"; msg: string }
  | { type: "done"; cost: number; counts: Record<string, number> }
  | { type: "error"; msg: string }
  | { type: "heartbeat" };
