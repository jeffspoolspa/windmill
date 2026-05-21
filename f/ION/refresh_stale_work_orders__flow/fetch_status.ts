import { chromium } from "playwright@1.40.0";

interface PrevResult { wo_numbers: string[]; }
interface IonResource { username: string; password: string; loginUrl: string; }

function parseWoStatus(html: string): { invoice_number: string | null; schedule_status: string | null } {
  let invoice_number: string | null = null;
  let schedule_status: string | null = null;

  // Try the STATUS legend first: "STATUS: WO# 4972018 - &nbsp;INVOICE #7816722"
  const legendMatch = html.match(/STATUS:\s+WO#\s+\d+\s+-\s+(?:&nbsp;)?INVOICE\s+#(\d+)/i);
  if (legendMatch) invoice_number = legendMatch[1];

  // Fallback: "Sync Status: Synced to QuickBooks 7816722"
  if (!invoice_number) {
    const syncMatch = html.match(/Synced to QuickBooks\s+(\d+)/i);
    if (syncMatch) invoice_number = syncMatch[1];
  }

  // Schedule status from the "Status" label-value cell.
  const statusCellMatch = html.match(
    /<td[^>]*>\s*Status\s*<\/td>\s*<td[^>]*>([\s\S]{0,500}?)<\/td>/i,
  );
  if (statusCellMatch) {
    const cellText = statusCellMatch[1]
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    // Longest first so "Closed - Not Invoiced" matches before "Closed".
    const known = [
      'Closed - Not Invoiced',
      'Not Scheduled',
      'Closed',
      'Scheduled',
      'Cancelled',
    ];
    for (const k of known) {
      const escaped = k.replace(/[-]/g, '\\-');
      const re = new RegExp(`(?:^|\\s)${escaped}(?:\\s|$|,)`, 'i');
      if (re.test(cellText)) { schedule_status = k; break; }
    }
  }

  return { invoice_number, schedule_status };
}

export async function main(previous_result: PrevResult, ion: IonResource) {
  const wos = previous_result.wo_numbers || [];
  if (wos.length === 0) {
    return { results: [], stats: { fetched: 0, errors: 0 } };
  }

  const browser = await chromium.launch({
    executablePath: '/usr/bin/chromium',
    args: ['--no-sandbox','--single-process','--no-zygote','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu'],
  });
  const context = await browser.newContext({ userAgent: 'Mozilla/5.0' });
  const page = await context.newPage();

  let cfClientId: string | undefined;
  page.on('request', (req: any) => {
    const m = req.url().match(/_cf_clientid=([A-F0-9]{32})/i);
    if (m && !cfClientId) cfClientId = m[1];
  });

  const results: any[] = [];
  let errors = 0;

  try {
    await page.goto(ion.loginUrl);
    await page.locator('#txtUserName').fill(ion.username);
    await page.locator('#txtPassword').fill(ion.password);
    await page.locator('button:has-text("Log In")').click();
    await page.waitForLoadState('networkidle', { timeout: 30000 });
    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 });
    await page.waitForTimeout(800);
    await page.locator('text=ION POOL CARE').click({ timeout: 5000 });
    await page.waitForLoadState('networkidle', { timeout: 45000 });
    const ionOrigin = new URL(page.url()).origin;

    for (const wo of wos) {
      const params = new URLSearchParams({
        id: wo,
        _cf_containerId: 'woInfo',
        _cf_nodebug: 'true',
        _cf_nocache: 'true',
      });
      if (cfClientId) params.set('_cf_clientid', cfClientId);
      const url = `${ionOrigin}/workorders/WOStatus.cfm?${params.toString()}`;
      try {
        const resp = await page.evaluate(async (u: string) => {
          const r = await fetch(u, {
            credentials: 'include',
            headers: { 'Referer': 'https://ionpoolcare.com/main.cfm', 'Accept': '*/*' },
          });
          return { status: r.status, body: await r.text() };
        }, url);
        const parsed = parseWoStatus(resp.body);
        results.push({ wo_number: wo, http_status: resp.status, ...parsed });
      } catch (e: any) {
        errors++;
        results.push({ wo_number: wo, http_status: 0, invoice_number: null, schedule_status: null, error: e.message });
      }
      await new Promise((r) => setTimeout(r, 200));
    }
  } finally {
    await browser.close();
  }

  return { results, stats: { fetched: results.length, errors } };
}
