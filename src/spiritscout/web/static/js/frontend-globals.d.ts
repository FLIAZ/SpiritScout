/** 未纳入本轮 checkJs 的原生脚本集成边界；事件和任务模型见 frontend-types.d.ts。 */
interface LocalizedText {
  (text: string): string;
  (parts: TemplateStringsArray, ...values: unknown[]): string;
}
declare const I18n: {t: LocalizedText; html: LocalizedText};
declare function $(selector: string): any;
declare const S: import('./frontend-types').StreamUIState;
declare const SessionObserver: {
  push(event: import('./frontend-types').AuditEvent): void;
  toolName(name: string): string;
  cleanText(value: unknown): string;
  resultSummary(detail: import('./frontend-types').AuditDetail): string;
};
declare const AgentWorkbench: {push(event: import('./frontend-types').AuditEvent): void};
declare const ToolParameters: {attach(element: HTMLElement,call: import('./frontend-types').ToolCall): void;reset(session: string): void;prune(): void};
declare const FileCards: {imageCard(image: unknown): string};
declare const TERMINAL: Set<string>;
declare function actorName(actor: string): string;
declare function esc(text: unknown): string;
declare const StreamText: {plain(target: HTMLElement,text: string,options?: {animate?: boolean}): void;markdown(target: HTMLElement,html: string,options?: {animate?: boolean}): void};
declare function mdRender(text: string): string;
declare function mdRenderInline(text: string): string;
declare function argBrief(name: string, args: Record<string, unknown>): string;
declare function traceActorSlot(actor: string): number;
declare function traceAppend(event: import('./frontend-types').AuditEvent, html: string, cls?: string): any;
declare function traceScroll(force?: boolean): void;
declare function traceStopFollowing(): void;
declare function traceIsPresent(row: any): boolean;
declare function traceBeginRender(options?: {isolate?: boolean}): any;
declare function traceEndRender(stage: any, commit: boolean, mode?: string): void;
interface Window { TraceViewport?: import('../../../../../frontend/session/viewport').TraceViewport }
declare function traceReset(options?: {preservePanels?: boolean}): void;
declare let traceStick: boolean;
declare function renderTopoActivity(): void;
declare function renderAuditEvent(event: import('./frontend-types').AuditEvent, options?: {observe?: boolean;eventId?: number;animate?: boolean}): void;
declare function api(url: string, options?: {signal?: AbortSignal}): Promise<import('./frontend-types').AuditPage>;
declare function refreshState(): void;
declare function scheduleStateRefresh(): void;
