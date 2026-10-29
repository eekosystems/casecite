import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', () => {
  const api = {
    request: vi.fn(),
    authFetch: vi.fn(),
    getToken: vi.fn().mockReturnValue('test-token'),
  };
  return { api, API_BASE_URL: 'http://localhost:8000/api/v1' };
});

import { api } from './client';
import './cases';

describe('api/cases', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.request as any).mockResolvedValue({});
  });

  it('getCaseOpinion says what kind of id it is sending', async () => {
    await (api as any).getCaseOpinion('123', 'opinion');
    expect(api.request).toHaveBeenCalledWith('/tools/cases/123?kind=opinion');

    await (api as any).getCaseOpinion('77', 'cluster');
    expect(api.request).toHaveBeenCalledWith('/tools/cases/77?kind=cluster');
  });

  it('getCaseOpinion leaves the kind to the server when not given', async () => {
    await (api as any).getCaseOpinion('123');
    expect(api.request).toHaveBeenCalledWith('/tools/cases/123');
  });
});
