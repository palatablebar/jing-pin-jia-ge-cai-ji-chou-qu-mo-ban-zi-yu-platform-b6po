import { useCallback, useEffect, useState } from 'react';
import { runsApi } from '../api/runs';
import { alertsApi } from '../api/alerts';
import { useAsync } from '../hooks/useAsync';
import { usePolling } from '../hooks/usePolling';
import F1Trend from '../components/common/F1Trend';
import FieldHitMatrix from '../components/common/FieldHitMatrix';
import StatusPill from '../components/common/StatusPill';
import { uiStore } from '../stores/uiStore';
import { errorMessage } from '../utils/errorMessage';
import {
  ATTEMPT_STATUS_LABEL,
  FIELD_STATUS_LABEL,
  RUN_STATUS_LABEL,
  needsTakeover,
} from '../constants/enums';
import { fmtDateTime } from '../utils/format';
import type { Alert, Run, RunAttempt } from '../types/models';

const ACTIVE_STATUSES = new Set(['queued', 'running', 'interrupted']);

function progressPercent(run: Run): number {
  if (run.result_complete) return 100;
  if (!run.progress_total) return 0;
  return Math.min(100, Math.round((run.progress_done / run.progress_total) * 100));
}

function ownerLabel(run: Run): string {
  if (!run.execution_owner) return '无归属';
  return run.lease_active ? run.execution_owner : `${run.execution_owner}（租约失效）`;
}

export default function Runs() {
  const { data, loading, error, reload } = useAsync(() => runsApi.list(), []);
  const [selected, setSelected] = useState<Run | null>(null);
  const [detail, setDetail] = useState<Run | null>(null);
  const [alert, setAlert] = useState<Alert | null>(null);
  const [busy, setBusy] = useState('');
  const hasActive = (data?.items ?? []).some((run) => ACTIVE_STATUSES.has(run.status));

  // 执行中 / 中断批次需要看实时接管进度
  usePolling(() => reload(), 4000, hasActive);

  const loadDetail = useCallback(async (runId: number) => {
    try {
      const fresh = await runsApi.get(runId);
      setDetail(fresh);
      setSelected((current) => (current && current.id === runId ? fresh : current));
    } catch {
      /* 详情轮询失败不打断列表 */
    }
  }, []);

  useEffect(() => {
    if (selected) loadDetail(selected.id);
  }, [selected?.id, loadDetail]);

  usePolling(() => {
    if (selected && ACTIVE_STATUSES.has(selected.status)) loadDetail(selected.id);
  }, 3000, Boolean(selected));

  async function openRun(run: Run) {
    setSelected(run);
    setDetail(null);
    setAlert(null);
    try {
      const alerts = await alertsApi.list();
      const matched = alerts.items.find((item) => item.snapshot_id === run.snapshot_id) ?? null;
      setAlert(matched);
    } catch {
      setAlert(null);
    }
    try {
      const fresh = await runsApi.get(run.id);
      setSelected(fresh);
      setDetail(fresh);
    } catch {
      /* 列表数据仍可用 */
    }
  }

  async function takeover(run: Run, force = false) {
    setBusy(`takeover-${run.id}`);
    try {
      const result = await runsApi.takeover(run.id, force);
      uiStore.push(
        'ok',
        result.takeover
          ? `已接管批次 #${result.id} 并续跑完成（F1 ${result.f1_score?.toFixed(2) ?? '—'}）`
          : `批次 #${result.id} 已是最新状态`,
      );
      setSelected(result);
      setDetail(result);
      reload();
    } catch (err) {
      const payload = (err as { payload?: { detail?: { reason?: string; execution_owner?: string } } }).payload;
      if (payload?.detail?.reason === 'batch_busy') {
        uiStore.push('error', `批次仍由 ${payload.detail.execution_owner ?? '其他实例'} 执行，稍后再试`);
      } else {
        uiStore.push('error', errorMessage((err as { payload?: unknown }).payload, '接管失败'));
      }
      reload();
    } finally {
      setBusy('');
    }
  }

  async function sweep() {
    setBusy('sweep');
    try {
      const result = await runsApi.sweepStale();
      uiStore.push('ok', result.stale ? `发现 ${result.stale} 个心跳超时批次` : '没有心跳超时的批次');
      reload();
    } catch (err) {
      uiStore.push('error', errorMessage((err as { payload?: unknown }).payload, '巡检失败'));
    } finally {
      setBusy('');
    }
  }

  if (error) return <div className="banner error">批次历史加载失败。</div>;
  if (loading && !data) return <div className="main">正在读取批次历史 …</div>;
  const runs = data?.items ?? [];
  const trend = data?.trend ?? [];
  const activeRuns = runs.filter((run) => ACTIVE_STATUSES.has(run.status));
  const current = detail ?? selected;

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="kicker">Runs / 抽取批次</div>
          <h1>批次历史与接管进度</h1>
          <p className="sub">
            同一批仅一个有效执行归属；实例失联后由新实例接管并从断点续跑，旧实例晚到结果不覆盖。
          </p>
        </div>
        <div className="head-actions">
          <StatusPill tone="active" label={`${runs.filter((run) => run.status === 'success').length} SUCCESS`} />
          <StatusPill tone="medium" label={`${activeRuns.length} ACTIVE`} />
          <StatusPill tone="failed" label={`${runs.filter((run) => run.status === 'failed').length} FAILED`} />
          <button className="btn btn-ghost" onClick={sweep} disabled={busy !== ''} data-testid="sweep-stale">
            {busy === 'sweep' ? '巡检中 …' : '巡检超时批次'}
          </button>
        </div>
      </div>

      {activeRuns.length ? (
        <div className="banner" data-testid="active-batches">
          当前 {activeRuns.length} 个批次未结束：
          {activeRuns.map((run) => (
            <span key={run.id} className="mono" style={{ marginLeft: 6 }}>
              #{run.id} {RUN_STATUS_LABEL[run.status]} {progressPercent(run)}%
            </span>
          ))}
        </div>
      ) : null}

      <div className="panel" style={{ marginBottom: '1.1rem' }}>
        <div className="panel-head">
          <h2>F1 趋势</h2>
          <span className="meta">仅完整结果 · 最近 {trend.length} 条批次</span>
        </div>
        <div className="panel-body">
          <F1Trend points={trend} />
        </div>
      </div>

      <div className="split">
        <div className="panel">
          <div className="panel-head">
            <h2>批次列表</h2>
            <span className="meta">共 {data?.total ?? 0} 条</span>
          </div>
          <div className="panel-body" style={{ padding: 0 }}>
            <table className="data">
              <thead>
                <tr>
                  <th>#</th>
                  <th>站点</th>
                  <th>模板</th>
                  <th>状态</th>
                  <th>归属</th>
                  <th>进度</th>
                  <th>命中</th>
                  <th>F1</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="clickable" onClick={() => openRun(run)} data-testid={`run-row-${run.id}`}>
                    <td className="num">{run.id}</td>
                    <td>{run.site_name}</td>
                    <td className="mono">v{run.template_version}</td>
                    <td>
                      <StatusPill tone={run.status} label={RUN_STATUS_LABEL[run.status]} />
                    </td>
                    <td className="mono wrap" title={run.execution_owner ?? '无归属'}>
                      {ownerLabel(run)}
                    </td>
                    <td className="num" data-testid={`run-progress-${run.id}`}>
                      <div className={`run-progress${ACTIVE_STATUSES.has(run.status) ? ' active' : ''}`}>
                        <span style={{ width: `${progressPercent(run)}%` }} />
                      </div>
                      <small>
                        {run.progress_done}/{run.progress_total || '—'}
                      </small>
                    </td>
                    <td className="num">
                      {run.hit_fields}/{run.hit_fields + run.missing_fields}
                    </td>
                    <td className="num">{run.f1_score?.toFixed(2) ?? '—'}</td>
                    <td className="mono">{fmtDateTime(run.finished_at ?? run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>批次详情</h2>
            <span className="meta">{current ? `#${current.id}` : '选择左侧一行'}</span>
          </div>
          <div className="panel-body">
            {current ? (
              <div>
                <dl className="kv" style={{ marginBottom: '0.8rem' }}>
                  <dt>站点 / 模板</dt>
                  <dd>
                    {current.site_name} · {current.template_name} v{current.template_version}
                  </dd>
                  <dt>最终状态</dt>
                  <dd>
                    <StatusPill tone={current.status} label={RUN_STATUS_LABEL[current.status]} />{' '}
                    {current.result_complete ? '结果完整' : '结果未完成'} · F1 {current.f1_score?.toFixed(4) ?? '—'}
                  </dd>
                  <dt>执行归属</dt>
                  <dd className="mono" data-testid="run-owner">
                    {ownerLabel(current)} · token {current.lease_token}
                    {current.lease_active ? (
                      <span style={{ color: 'var(--copper)' }}> · 心跳 {fmtDateTime(current.heartbeat_at)}</span>
                    ) : null}
                  </dd>
                  <dt>接管进度</dt>
                  <dd data-testid="run-progress-detail">
                    <div className="run-progress" style={{ marginBottom: 3 }}>
                      <span style={{ width: `${progressPercent(current)}%` }} />
                    </div>
                    {current.progress_done}/{current.progress_total || '—'} 字段 · 尝试 {current.attempt_count} 次
                  </dd>
                  <dt>触发者</dt>
                  <dd className="mono">{current.triggered_by}</dd>
                  <dt>错误信息</dt>
                  <dd>{current.error_message ?? '—'}</dd>
                </dl>

                {needsTakeover(current) ? (
                  <div className="banner" data-testid="takeover-banner" style={{ display: 'flex', justifyContent: 'space-between', gap: '0.6rem', alignItems: 'center' }}>
                    <span>
                      {current.status === 'interrupted'
                        ? '该批次执行实例已失联，可由当前实例接管并从断点续跑。'
                        : '该批次执行归属已失效（租约过期），可接管续跑。'}
                    </span>
                    <button
                      className="btn btn-primary btn-sm"
                      onClick={() => takeover(current)}
                      disabled={busy !== ''}
                      data-testid="takeover-run"
                    >
                      {busy === `takeover-${current.id}` ? '接管中 …' : '接管并续跑'}
                    </button>
                  </div>
                ) : null}

                <h3 style={{ fontSize: '0.72rem', letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--ink-soft)', margin: '0.9rem 0 0.45rem' }}>
                  执行时间线
                </h3>
                {(current.attempts ?? []).length ? (
                  <ol className="attempt-timeline" data-testid="attempt-timeline">
                    {(current.attempts ?? []).map((attempt: RunAttempt) => (
                      <li key={attempt.id} className={`attempt attempt-${attempt.status}`}>
                        <div className="attempt-head">
                          <span className="mono">#{attempt.attempt_no}</span>
                          <span className="mono wrap">{attempt.instance_id}</span>
                          <StatusPill tone={attempt.status} label={ATTEMPT_STATUS_LABEL[attempt.status]} />
                        </div>
                        <div className="attempt-meta">
                          进度 {attempt.progress_done}/{attempt.progress_total || '—'}
                          {attempt.resumed_from ? ` · 从第 ${attempt.resumed_from + 1} 个字段续跑` : ''}
                          {' · '}
                          {fmtDateTime(attempt.started_at)}
                          {attempt.finished_at ? ` → ${fmtDateTime(attempt.finished_at)}` : ''}
                          {attempt.message ? ` · ${attempt.message}` : ''}
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p style={{ fontSize: '0.72rem', color: 'var(--ink-soft)' }}>暂无执行尝试记录。</p>
                )}

                {alert ? (
                  <>
                    <p style={{ fontSize: '0.7rem', color: 'var(--ink-soft)', margin: '0.7rem 0 0.5rem' }}>
                      关联告警 #{alert.id} · 漂移 {alert.drift_score.toFixed(2)}（{alert.severity}）
                    </p>
                    <FieldHitMatrix rows={alert.diagnosis.field_matrix} compact />
                  </>
                ) : (
                  <table className="data" style={{ marginTop: '0.6rem' }}>
                    <thead>
                      <tr>
                        <th>字段</th>
                        <th>抽取值</th>
                        <th>状态</th>
                        <th>选择器</th>
                      </tr>
                    </thead>
                    <tbody>
                      {current.field_results.map((field) => (
                        <tr key={field.field}>
                          <td>{field.label}</td>
                          <td className="mono">{field.value ?? '—'}</td>
                          <td>
                            <StatusPill tone={field.status} label={FIELD_STATUS_LABEL[field.status]} />
                          </td>
                          <td className="mono wrap">{field.matched_selector ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            ) : (
              <p style={{ color: 'var(--ink-soft)', fontSize: '0.8rem' }}>
                点击批次行查看执行归属、接管进度与字段矩阵。中断批次可由存活实例接管续跑。
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
