import { api } from './client';
import type { CaseDetail } from './types';

Object.assign(api, {
  // `kind` says what the id is: 'opinion' for a citation's opinion_id,
  // 'cluster' for a search-result id. Omitted, the server tries cluster
  // first, then opinion — which can resolve an opinion id to the wrong case.
  async getCaseOpinion(opinionId: string, kind?: 'opinion' | 'cluster'): Promise<CaseDetail> {
    const query = kind ? `?kind=${kind}` : '';
    return api.request(`/tools/cases/${opinionId}${query}`);
  },
});
