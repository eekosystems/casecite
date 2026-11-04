import { describe, expect, it } from 'vitest';
import { bundledServerUrl } from './store';

describe('bundledServerUrl', () => {
  it('is the pane origin when the pane is served by the CaseCite image at /addin/', () => {
    expect(bundledServerUrl({ origin: 'https://casecite.firm.test', pathname: '/addin/' })).toBe('https://casecite.firm.test');
    expect(bundledServerUrl({ origin: 'https://casecite.firm.test', pathname: '/addin/index.html' })).toBe('https://casecite.firm.test');
    expect(bundledServerUrl({ origin: 'https://casecite.firm.test', pathname: '/addin' })).toBe('https://casecite.firm.test');
  });

  it('is empty when the pane is hosted anywhere else', () => {
    expect(bundledServerUrl({ origin: 'https://addin.firm.test', pathname: '/' })).toBe('');
    expect(bundledServerUrl({ origin: 'https://localhost:3200', pathname: '/index.html' })).toBe('');
    expect(bundledServerUrl({ origin: 'https://x', pathname: '/addins/' })).toBe('');
    expect(bundledServerUrl(null)).toBe('');
  });
});
