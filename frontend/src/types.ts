export type Category = 'person' | 'identity' | 'address' | 'phone';
export interface Rect { x: number; y: number; width: number; height: number }
export interface CustomField { id: string; value: string; enabled: boolean }
export interface RuleSet { categories: Category[]; custom_fields: CustomField[] }
export interface Mask { id: string; box: Rect; source: 'auto' | 'manual'; reasons: string[]; fallback: boolean }
export interface EditState { manual: { id: string; box: Rect }[]; excluded: string[]; overrides: Record<string, Rect> }
export interface ProcessingProgress {
  stage: 'queued' | 'preparing' | 'loading' | 'detecting' | 'recognizing' | 'analyzing' | 'finishing' | 'done';
  label: string; step: number; steps: number; elapsed_seconds: number; waiting_seconds: number;
  completed: number; total: number | null; percent: number | null;
  unit?: 'tile' | 'strip' | null;
  eta_min_seconds: number | null; eta_max_seconds: number | null;
  eta_scope: 'stage' | 'image'; estimate_basis: 'rough' | 'session' | 'current'; overdue: boolean;
}
export interface ImageRecord {
  id: string; name: string; number: number; width: number; height: number;
  status: 'queued' | 'processing' | 'review' | 'error' | 'waiting_memory'; message: string; revision: number;
  confirmed: boolean; masks: Mask[]; automatic_count: number; excluded_count: number;
  edits: EditState; line_count: number; progress?: ProcessingProgress | null; queue_position?: number;
  preview_ready?: boolean; cache?: 'memory' | 'encrypted';
  resource_issue?: ResourceIssue | null;
}
export interface ResourceIssue {
  resource: 'memory' | 'disk'; code: string; stage: string;
  available_bytes?: number; commit_available_bytes?: number | null;
  required_bytes?: number; release_bytes?: number; estimated?: boolean;
}
export interface MemoryStatus {
  mode: 'normal' | 'saving'; available_bytes: number; commit_available_bytes: number | null;
  waiting_image_id: string | null; message: string | null;
  processing?: boolean; workspace_estimate_known?: boolean;
}
export type PerformanceMode = 'low_impact' | 'high_performance';
export interface RuntimeStatus {
  mode: PerformanceMode; backend: 'pending' | 'starting' | 'accelerated' | 'compatible';
  requested_mode?: PerformanceMode; active_mode?: PerformanceMode; pending?: boolean; target_threads?: number;
  threads?: number; cpu_budget_percent?: number; cpu_cap_applied?: boolean; below_normal?: boolean; ecoqos?: boolean;
  warning: string | null; fallback_reason: string | null;
  power_applied?: boolean;
}
export interface Health { models: { ready: boolean; missing: string[] }; settings_error: string | null; local_only: boolean; runtime?: RuntimeStatus | null; memory?: MemoryStatus }
export type Tool = 'select' | 'draw' | 'pan';
