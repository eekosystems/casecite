import { test, expect } from '@playwright/test';
import { API, freshUser, makePdf, registerUser, signInThroughUi } from './helpers';

/**
 * The core journey, against the real backend: sign in, upload a real PDF, see
 * it indexed, ask a question about it, read an answer whose citation was
 * verified against the file, open the citation, delete the document, and
 * find the deletion in the audit log. The mock provider behind the backend
 * is deterministic, so the badges and texts asserted here are the product's
 * real behaviour on real data paths, not route mocks.
 */
const FILE_NAME = 'marlow-tidewater-msa.pdf';
const CLAUSE =
  'Either Party may terminate this Agreement upon thirty days written notice to the other Party if the other Party commits a Material Breach and fails to cure it within that period.';
const PDF = makePdf([
  'MASTER SERVICES AGREEMENT',
  'between Marlow Holdings LLC and Tidewater Logistics Inc.',
  '',
  '5.2 Termination for Cause.',
  CLAUSE,
  '',
  '9.1 Governing Law. This Agreement is governed by the laws of the State of Delaware.',
]);

test.describe('Research journey (live backend)', () => {
  test.use({ viewport: { width: 1400, height: 1000 } });
  test.setTimeout(180_000);

  test('upload, index, ask, verified citation, delete, audit', async ({ page, request }) => {
    const user = freshUser();
    const roles = await registerUser(request, user);

    await page.goto('/');
    await signInThroughUi(page, user);

    // ── Upload a real PDF and watch it become searchable ─────────────────
    await page.goto('/documents');
    await expect(page.getByRole('heading', { name: 'Knowledge Base' })).toBeVisible();
    await page.getByLabel('Upload documents').setInputFiles({
      name: FILE_NAME,
      mimeType: 'application/pdf',
      buffer: PDF,
    });
    const card = page.locator('[class*="docCard"]', { hasText: FILE_NAME });
    await expect(card).toBeVisible({ timeout: 60_000 });
    await expect(card).toContainText(/\d+ chunks/, { timeout: 60_000 });
    await expect(card).not.toContainText(/Failed/);

    // ── Ask about it ──────────────────────────────────────────────────────
    await page.goto('/research');
    const box = page.getByLabel('Ask or strategize across your documents');
    await box.fill('Can either party terminate the agreement immediately for a material breach?');
    await page.getByRole('button', { name: 'Send question' }).click();
    await expect(page.getByText(/Based on your documents:/)).toBeVisible({ timeout: 90_000 });

    // ── The answer's citation was verified against the file ──────────────
    const sources = page.locator('aside#right-panel');
    await expect(sources.getByText(FILE_NAME).first()).toBeVisible({ timeout: 30_000 });
    await expect(sources.getByText('Supported').first()).toBeVisible();
    await sources.getByText(FILE_NAME).first().click();
    const modal = page.getByRole('dialog');
    await expect(modal.getByTestId('verification-badge')).toContainText('Supported');
    await expect(modal.getByText(/Material Breach/)).toBeVisible();
    await modal.getByRole('button', { name: 'Close' }).first().click();

    // ── Delete it; the deletion is in the audit trail ─────────────────────
    await page.goto('/documents');
    await page.locator('[class*="docCard"]', { hasText: FILE_NAME }).getByTitle('Delete document').click();
    const confirm = page.getByRole('dialog');
    await expect(confirm.getByText('Delete Document')).toBeVisible();
    await confirm.getByRole('button', { name: /^Delete$|^Confirm$/ }).click();
    await expect(page.getByText(`Deleted ${FILE_NAME}`)).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('No documents yet')).toBeVisible({ timeout: 30_000 });

    // The audit trail (admin only: the first account on a fresh database).
    if (!roles.includes('admin')) {
      test.info().annotations.push({ type: 'note', description: 'not the first account: audit check skipped' });
      return;
    }
    const audit = await request.get(`${API}/admin/audit/logs?event_type=document.delete&limit=5`, {
      headers: { Authorization: `Bearer ${await tokenFor(request, user)}` },
    });
    expect(audit.status(), await audit.text()).toBe(200);
    expect(JSON.stringify(await audit.json())).toContain('document.delete');
  });
});

async function tokenFor(request: import('@playwright/test').APIRequestContext, user: { email: string; password: string }): Promise<string> {
  const response = await request.post(`${API}/auth/login`, {
    data: { email: user.email, password: user.password },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).access_token;
}
