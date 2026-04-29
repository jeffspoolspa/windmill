//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0

import { chromium } from "playwright@1.40.0";
import { parse } from "node-html-parser";

type IonResource = {
  username: string;
  password: string;
  loginUrl: string;
};

const REPORT_SELECTORS: Record<string, string> = {
  consumables_detail: 'a[href*="consumablesDetailByTech.cfm"]',
  service_summary: 'a[href*="serviceSummary.cfm"]',
};

export async function main(
  ion: IonResource,
  report_name: "consumables_detail" | "service_summary",
  start_date: string,
  end_date: string
) {
  start_date = start_date.replace(/"/g, '');
  end_date = end_date.replace(/"/g, '');
  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  
  const context = await browser.newContext({
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    acceptDownloads: true
  });
  const page = await context.newPage();

  await page.goto(ion.loginUrl);
  await page.locator('#txtUserName').fill(ion.username);
  await page.locator('#txtPassword').fill(ion.password);
  await page.locator('button:has-text("Log In")').click();
  await page.waitForLoadState('networkidle');

  await page.locator('button[data-bs-target="#navbarToggleContent"]').click();
  await page.locator('text=ION POOL CARE').click();
  await page.waitForLoadState('networkidle');

  try {
    await page.locator('#MyPrintWin .x-tool-close').click({ timeout: 2000 });
  } catch {}

  await page.locator('#menuItem13 a').click();
  await page.locator('.ovalbutton:has-text("Service Reports")').click();
  await page.waitForTimeout(1000);

  await page.locator('#rptStart').evaluate((el, val) => {
    (el as HTMLInputElement).value = val;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, start_date);

  await page.locator('#rptEnd').evaluate((el, val) => {
    (el as HTMLInputElement).value = val;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, end_date);
  await page.waitForTimeout(2000);

  const downloadPromise = page.waitForEvent('download');
  await page.locator(REPORT_SELECTORS[report_name]).first().click();
  const download = await downloadPromise;

  const path = await download.path();
  const html = await Bun.file(path!).text();

  await browser.close();

  const root = parse(html);
  const table = root.querySelector('table');
  if (!table) throw new Error('No table found in downloaded HTML');

  const rows = table.querySelectorAll('tr');
  const rawData = rows.map(row => {
    const cells = row.querySelectorAll('td, th');
    return cells.map(cell => cell.text.trim());
  });

  return { raw_table: rawData };
}
