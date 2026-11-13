import { http } from './client';
import type { Run, RunAction, TrendPoint } from '../types/models';

export const runsApi = {
  list: (siteId?: number) =>
    http.get<{ items: Run[]; total: number; trend: TrendPoint[] }>(`/runs${siteId ? `?site_id=${siteId}` : ''}`),
  get: (id: number) => http.get<Run>(`/runs/${id}`),
  execute: (templateId: number, snapshotId: number) =>
    http.post<RunAction>('/runs', { template_id: templateId, snapshot_id: snapshotId }),
  takeover: (id: number, force = false) => http.post<RunAction>(`/runs/${id}/takeover`, { force }),
  sweepStale: () => http.post<{ stale: number; items: { run_id: number }[] }>('/runs/sweep-stale'),
};
