//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Uses the shared background session (f/ION/session_cache, kept warm by
// f/ION/session_keepalive every 10min + the GitHub Actions minter on death)
// instead of logging in itself. Only touches chromium (via getOrRefreshSession's
// fallback) if that cache is somehow empty/stale -- normally this is pure HTTP.
//
// NOTE: deliberately does NOT import from /f/ION/_lib/session directly --
// importing both session_cache and session in the same file corrupts
// Windmill's relative-import bundler (substring collision on "session").
// Cookie-header building is inlined below instead.

import { parse } from "node-html-parser";
import { mkdir } from "fs/promises";
import * as wmill from "windmill-client";
import { getOrRefreshSession } from "/f/ION/_lib/session_cache";

type IonResource = { username: string; password: string; loginUrl: string };

interface IonCookie { name: string; value: string; domain: string; path: string }
interface IonSession { cookies: IonCookie[]; cfClientId?: string; ionOrigin: string }

function cookieHeader(session: IonSession): string {
  const host = new URL(session.ionOrigin).hostname;
  return session.cookies
    .filter((c) => { const d = c.domain.replace(/^\./, ''); return host === d || host.endsWith('.' + d); })
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

async function sessionFetchText(session: IonSession, url: string): Promise<string> {
  const res = await fetch(url, {
    headers: {
      Cookie: cookieHeader(session),
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      Accept: 'text/html, */*',
    },
  });
  if (!res.ok) {
    const preview = (await res.text()).slice(0, 300);
    throw new Error(`fetch ${url} -> HTTP ${res.status}: ${preview}`);
  }
  return res.text();
}

function toIsoDate(dateStr: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return dateStr;
  const parts = dateStr.split('/');
  if (parts.length === 3) return `${parts[2]}-${parts[0].padStart(2,'0')}-${parts[1].padStart(2,'0')}`;
  return new Date(dateStr).toISOString().split('T')[0];
}

export async function main(
  wo_status_1: string,
  wo_status_2: string,
  start_date?: string
) {
  const isoStartDate = start_date ? toIsoDate(start_date) : '';

  console.log('========================================');
  console.log('WORK ORDERS - SESSION-CACHE FETCH (no inline login)');
  console.log(`ScheduleStart: ${isoStartDate}`);
  console.log('========================================');

  const ion: IonResource = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  };

  const session = (await getOrRefreshSession(ion)) as unknown as IonSession;
  console.log(`  session ionOrigin: ${session.ionOrigin}`);
  console.log(`  _cf_clientid: ${session.cfClientId || 'NONE'}`);

  await mkdir('./shared', { recursive: true });

  console.log('\nSTEP 1: FETCH REPORT PICKER');
  const pickerParams = new URLSearchParams({
    Office: '',
    Technician: '',
    ScheduleStart: isoStartDate,
    ScheduleEnd: '',
    WOType: '',
    WOTemplate: '',
    WOStatus: '',
    ScheduleStatus: '',
    ApprovalStatus: '',
    CreatedStart: '',
    CreatedEnd: '',
    CompletedStart: '',
    CompletedEnd: '',
    _cf_containerId: 'rptDetail',
    _cf_nodebug: 'true',
    _cf_nocache: 'true',
    _cf_rc: '1',
  });
  if (session.cfClientId) pickerParams.set('_cf_clientid', session.cfClientId);

  const pickerUrl = `${session.ionOrigin}/reports/woReports.cfm?${pickerParams.toString()}`;
  console.log(`  picker URL: ${pickerUrl.substring(0, 120)}...`);

  const pickerBody = await sessionFetchText(session, pickerUrl);

  const pickerRoot = parse(pickerBody);
  const allLinks = pickerRoot.querySelectorAll('a');
  console.log(`  total links on picker: ${allLinks.length}`);

  let downloadHref: string | null = null;
  for (const link of allLinks) {
    const href = link.getAttribute('href') || '';
    if (href.includes('WorkOrderDetail') && !downloadHref) {
      downloadHref = href;
    }
  }

  if (!downloadHref) {
    console.log('  NO WorkOrderDetail link found. Saving picker HTML...');
    await Bun.write('./shared/picker_page.html', pickerBody);
    throw new Error('No WorkOrderDetail.cfm link found on picker page');
  }

  console.log(`  download link found: ${downloadHref.substring(0, 120)}...`);

  const reportDataUrl = downloadHref.startsWith('http') ? downloadHref : `${session.ionOrigin}${downloadHref.startsWith('/') ? '' : '/reports/'}${downloadHref}`;
  console.log(`  full report URL: ${reportDataUrl.substring(0, 140)}...`);

  console.log('\nSTEP 2: FETCH REPORT DATA');
  const reportBody = await sessionFetchText(session, reportDataUrl);
  console.log(`  body length: ${reportBody.length}`);

  await Bun.write('./shared/raw_report.html', reportBody);
  console.log('  saved raw_report.html');

  console.log('\nSTEP 3: PARSE');
  const root = parse(reportBody);

  const allTables = root.querySelectorAll('table');
  console.log(`  tables found: ${allTables.length}`);

  let dataTable = null;
  for (const t of allTables) {
    if (t.toString().includes('WO #') || t.toString().includes('WO#')) {
      dataTable = t;
      console.log('  found data table via WO # header');
      break;
    }
  }
  if (!dataTable && allTables.length > 0) {
    let maxRows = 0;
    for (const t of allTables) {
      const c = t.querySelectorAll('tr').length;
      if (c > maxRows) { maxRows = c; dataTable = t; }
    }
    console.log(`  using largest table (${maxRows} rows)`);
  }

  if (!dataTable) {
    console.log(`  response preview: ${reportBody.substring(0, 2000)}`);
    throw new Error('No data table found in report');
  }

  const rows = dataTable.querySelectorAll('tr');
  const rawData = rows.map((row: any) => {
    const cells = row.querySelectorAll('td, th');
    return cells.map((cell: any) => cell.text.trim());
  });

  console.log(`  total rows: ${rawData.length}`);
  for (let i = 0; i < Math.min(rawData.length, 6); i++) {
    console.log(`  row[${i}] (${rawData[i].length} cells): ${JSON.stringify(rawData[i].slice(0, 5))}...`);
  }

  const report1Path = './shared/report_1.json';
  await Bun.write(report1Path, JSON.stringify({ status: wo_status_1, raw_table: rawData }, null, 2));

  const dataRowCount = Math.max(0, rawData.length - 4);
  console.log(`\nSUCCESS - ${dataRowCount} data rows`);

  return {
    success: true,
    method: 'session_cache_http_fetch',
    report_1: { status: wo_status_1, filepath: report1Path, row_count: rawData.length },
    data_row_count: dataRowCount,
    report_data_url: reportDataUrl,
  };
}