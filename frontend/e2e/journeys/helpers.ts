import { expect, type APIRequestContext, type Page } from '@playwright/test';

/**
 * Helpers for the journey suite: a REAL backend (no route mocks), reached at
 * BACKEND_URL, with the mock provider from scripts/mock_llm_server.py behind
 * it. See frontend/e2e/journeys/README.md for how to run it.
 */
export const BACKEND_URL = process.env.JOURNEYS_BACKEND_URL || 'http://localhost:8000';
export const API = `${BACKEND_URL}/api/v1`;

export const PASSWORD = 'Journey-Passw0rd!-ok';

export interface JourneyUser {
  email: string;
  name: string;
  password: string;
}

export function freshUser(prefix = 'journey'): JourneyUser {
  const tag = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  return { email: `${prefix}-${tag}@example.com`, name: 'Journey User', password: PASSWORD };
}

/** Register through the real API (DEBUG instances accept registration). */
export async function registerUser(request: APIRequestContext, user: JourneyUser): Promise<string[]> {
  const csrf = await request.get(`${API}/csrf-token`);
  expect(csrf.ok()).toBeTruthy();
  const token = (await csrf.json()).csrf_token as string;
  const response = await request.post(`${API}/auth/register`, {
    headers: { 'X-CSRF-Token': token },
    data: { email: user.email, name: user.name, password: user.password, company: 'Journey LLP' },
  });
  expect(response.status(), await response.text()).toBe(201);
  // The first account on an empty database is the admin (CI always is).
  return ((await response.json()).user?.roles ?? []) as string[];
}

/** Sign in through the header dialog, as a person would. */
export async function signInThroughUi(page: Page, user: JourneyUser): Promise<void> {
  const header = page.locator('header[data-layout="header"]');
  await header.getByRole('button', { name: /Sign In/i }).click();
  const modal = page.getByRole('dialog');
  await expect(modal.getByRole('heading', { name: 'Sign In' })).toBeVisible();
  await modal.locator('input[type="email"]').fill(user.email);
  await modal.locator('input[type="password"]').fill(user.password);
  await modal.getByRole('button', { name: 'Sign In', exact: true }).click();
  await expect(modal).toHaveCount(0);
  await expect(header.getByText(user.email.split('@')[0])).toBeVisible();
}

function pdfEscape(text: string): string {
  return text.replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
}

/**
 * A small, valid, text-bearing PDF (one page, Helvetica) built by hand, so
 * the suite needs no fixture binaries and the text layer is exactly known.
 */
export function makePdf(lines: string[]): Buffer {
  const content = ['BT', '/F1 11 Tf', '72 740 Td', '14 TL']
    .concat(lines.map((line, i) => `${i === 0 ? '' : 'T* '}(${pdfEscape(line)}) Tj`))
    .concat(['ET'])
    .join('\n');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    `<< /Length ${Buffer.byteLength(content, 'latin1')} >>\nstream\n${content}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let body = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((obj, i) => {
    offsets.push(Buffer.byteLength(body, 'latin1'));
    body += `${i + 1} 0 obj\n${obj}\nendobj\n`;
  });
  const xref = Buffer.byteLength(body, 'latin1');
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) body += `${offset.toString().padStart(10, '0')} 00000 n \n`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body, 'latin1');
}
