/** 前端共享枚举（后端 src/constants/enums.py 对应同一组值）。 */
import type { AlertStatus, AttemptStatus, FieldStatus, RunStatus, Severity, TemplateStatus } from '../types/models';

export const SEVERITY_LABEL: Record<Severity, string> = {
  high: '高危',
  medium: '关注',
  low: '观察',
  none: '正常',
};

export const ALERT_STATUS_LABEL: Record<AlertStatus, string> = {
  open: '待处置',
  accepted: '已接受',
  dismissed: '已驳回',
};

export const TEMPLATE_STATUS_LABEL: Record<TemplateStatus, string> = {
  draft: '草稿',
  active: '生效中',
  deprecated: '已归档',
};

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: '排队中',
  running: '执行中',
  interrupted: '已中断',
  success: '成功',
  failed: '失败',
};

export const ATTEMPT_STATUS_LABEL: Record<AttemptStatus, string> = {
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  interrupted: '心跳超时',
  superseded: '被接管',
  rejected: '晚到被拒',
};

/** 终态（不会再被新实例改写） */
export const RUN_TERMINAL: RunStatus[] = ['success', 'failed'];

/** 批次是否需要/可以被接管续跑 */
export function needsTakeover(run: { status: RunStatus; result_complete: boolean; lease_active: boolean }): boolean {
  if (run.result_complete) return false;
  return run.status === 'interrupted' || (run.status === 'running' && !run.lease_active);
}

export const PENDING_REASON_LABEL: Record<string, string> = {
  batch_executing: '批次执行中（未进入日报）',
  batch_interrupted: '批次已中断，等待接管续跑',
  no_complete_run: '暂无完整批次',
};

export const FIELD_STATUS_LABEL: Record<FieldStatus, string> = {
  hit: '命中',
  wrong: '错位',
  miss: '缺失',
};

export const CANDIDATE_KIND_LABEL: Record<string, string> = {
  class_relaxed: 'class 容错变体',
  sibling_index: '兄弟索引',
  text_anchor: '文本锚点 XPath',
  fuzzy_text: 'tag + 文本模糊',
  existing: '现行规则',
};

export const DRIFT_WEIGHTS = [
  { key: 'path_jaccard', label: '结构路径 Jaccard', weight: 0.4, inverted: true },
  { key: 'node_ratio_delta', label: '节点数变化率', weight: 0.2, inverted: false },
  { key: 'text_similarity', label: '正文本相似度', weight: 0.2, inverted: true },
  { key: 'field_miss_delta', label: '字段命中率下降', weight: 0.2, inverted: false },
];
