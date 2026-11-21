/** 审计字节游标；不是事件 seq，也不是数组下标。 */
export type AuditCursor = number;
export type TaskStatus = 'running' | 'waiting' | 'done' | 'failed' | 'cancelled' | 'pruned';
export interface ToolCall {
  id?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  preview?: string;
}
export interface Exploration {
  intent?: string;
  attempt?: string;
  state?: string;
  [key: string]: unknown;
}
export interface AuditDetail {
  action?: string; mode?: string; agent_id?: string; task_id?: string;
  route_id?: string; route?: string; call_id?: string; turn?: number;
  kind?: string; text?: string; title?: string; angle?: string; target?: string;
  type?: string; cache_scope?: string; response_format?: string;
  user_direction_hints?: string[]; exploration?: Exploration;
  calls?: ToolCall[]; id?: string; name?: string; ok?: boolean;
  result?: unknown; data?: unknown; error?: string; note?: string;
  output_file?: string; invoked_tool?: string; output_complete?: boolean; result_truncated?: boolean;
  same_raw_output?: boolean; parameter_error?: boolean; failure_evidence?: {timed_out?: boolean};
  question?: string; answer?: string; reason?: string; message?: string; streamed?: boolean;
  summary?: string | {conclusion?: string; summary?: string; routes_total?: number; routes_done?: number; status?: string};
  conclusions?: {route?: string; text?: string}[];
  skill?: {id?: string; skill_id?: string; version?: string; revision?: string; path?: string; paths?: string[]; [key: string]: unknown};
  images?: { [key: string]: unknown }[];
  content?: string; message_id?: string; tokens?: number;
}
/** 兼容旧审计省略 actor/时间/detail；type 保留扩展事件，交由消费者按职责筛选。 */
export interface AuditEvent {
  type: string;
  actor?: string;
  ts?: string;
  seq?: number;
  detail?: AuditDetail;
}
export interface AuditItem { id: AuditCursor; event: AuditEvent }
export interface AuditPage {
  items: AuditItem[];
  older_cursor: AuditCursor;
  cursor: AuditCursor;
  has_more: boolean;
  activity_count?: number;
}
/** 原文页可以被缓存淘汰，页边界仍保留以便重新读取。 */
export interface HistoryPage extends Omit<AuditPage,'older_cursor'> { older_cursor?: AuditCursor; start: AuditCursor; touched?: number }
export interface TraceStep {
  key: string; route: string; turn: number; event: AuditEvent;
  texts: Map<string,string>; status: TaskStatus; progress: string;
  title?: string; intentKey?: string; exploration?: Exploration;
}
export interface TraceSegment {
  key: string; stageKey: string; stepKey: string; route: string; turn: number;
  intent: string; epoch: number; event: AuditEvent;
  startId: AuditCursor | null; endId: AuditCursor | null;
  texts: Map<string,string>; toolKeys: Set<string>; records: Map<string,string>;
  status: TaskStatus; progress: string; title: string; version: number;
  declaredIntent?: string;
}
export interface TraceTool {
  key: string; id?: string; name?: string; call: ToolCall; status?: string;
  result?: string; note?: string; version: number; missingCall?: boolean;
  stepKey: string; intentKey?: string; route?: string; skill?: AuditDetail['skill']; images?: {[key:string]:unknown}[]; clipped?: boolean;
  callCursor?: number; resultCursor?: number;
  output?: {cursor?: number; complete?: boolean; truncated?: boolean; possiblyTruncated?: boolean; same?: boolean};
  failure?: "parameters" | "timeout";
}
export interface TraceTask {
  key: string; id: string; actor: string; title: string; status: TaskStatus;
  progress: string; tools: Map<string,TraceTool>; texts: Map<string,string>;
  completedTexts: Set<string>; steps: Map<string,TraceStep>; segments: Map<string,TraceSegment>;
  activeIntents: Map<string,TraceSegment>; activeSteps: Map<string,TraceStep>;
  routeAliases: Map<string,Set<string>>; conclusions: Map<string,string>; modelSteps: Map<string,string>;
  started: boolean; summary: string; updated: string; event: AuditEvent; version: number;
  currentSegmentKey: string; segmentSerial: number; startId: AuditCursor | null; endId: AuditCursor | null;
  singleExecutor?: boolean; directionHints?: string[]; target?: string; endEvent?: AuditEvent;
  waitingReason?: string; waitingReasons?: Map<string,string>; stats?: string; narrativeRevision?: number;
}
export interface TraceStore {
  tasks: Map<string,TraceTask>; routes: Map<string,Set<string>>; calls: Map<string,string>;
  singleActors: Map<string,{hints:string[]}>; machineCalls: Set<string>;
}

/** 仅声明本轮三个消费者使用的共享 UI 状态，不扩展后端会话状态枚举。 */
export interface StreamUIState {
  cur: string | null; view: string; tab: string; evSeen: AuditCursor;
  es: EventSource | null; esDog: ReturnType<typeof setInterval> | null; esTick: number;
  state?: {session?: {status: string; mode?: string; paused?: boolean}};
  think: Map<string,{chars:number;tail:string}>;
}
