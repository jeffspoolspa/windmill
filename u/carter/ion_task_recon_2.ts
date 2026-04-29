//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0";
import * as wmill from "windmill-client";
import { mkdir } from "fs/promises";
import { parse } from "node-html-parser";

export async function main() {
  const LOGIN_URL = await wmill.getVariable("f/ION/LOGIN_URL");
  const USERNAME = await wmill.getVariable("f/ION/USERNAME");
  const PASSWORD = await wmill.getVariable("f/ION/PASSWORD");
  const ROBYN_ID = "1351407";

  console.log("========================================");
  console.log("ION RECON 2: Robyn Cheek Tasks Deep Dive");
  console.log(`CustomerID: ${ROBYN_ID}`);
  console.log("========================================");

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox","--single-process","--no-zygote","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"],
  });
  const context = await browser.newContext({
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    acceptDownloads: true,
  });
  const page = await context.newPage();
  await mkdir("./shared", { recursive: true });

  // Capture ALL network requests
  const cfmRequests: any[] = [];
  page.on("request", (req: any) => {
    const url = req.url();
    const method = req.method();
    if (url.includes(".cfm") || url.includes("/tasks/") || url.includes("/task")) {
      const entry: any = { method, url: url.substring(0, 500) };
      if (method === "POST") {
        entry.postData = req.postData()?.substring(0, 3000) || null;
        entry.contentType = req.headers()?.["content-type"] || null;
      }
      cfmRequests.push(entry);
      console.log(`  [${method}] ${url.substring(0, 250)}`);
      if (entry.postData) console.log(`    body: ${entry.postData.substring(0, 500)}`);
    }
  });

  try {
    // ===== LOGIN + ION REDIRECT =====
    console.log("\n--- LOGIN + ION REDIRECT ---");
    await page.goto(LOGIN_URL);
    await page.locator("#txtUserName").fill(USERNAME);
    await page.locator("#txtPassword").fill(PASSWORD);
    await page.locator('button:has-text("Log In")').click();
    await page.waitForLoadState("networkidle", { timeout: 30000 });

    let cfClientId: string | undefined;
    page.on("request", (req: any) => {
      const url = req.url();
      if (url.includes("_cf_clientid=") && !cfClientId) {
        const match = url.match(/_cf_clientid=([A-F0-9]{32})/i);
        if (match) cfClientId = match[1];
      }
    });

    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 });
    await page.waitForTimeout(1000);
    await page.locator("text=ION POOL CARE").click({ timeout: 5000 });
    await page.waitForLoadState("networkidle", { timeout: 45000 });
    const ionOrigin = new URL(page.url()).origin;
    console.log(`  ION: ${ionOrigin}`);

    // Also extract _cf_clientid from page if not from network
    if (!cfClientId) {
      cfClientId = await page.evaluate(() => {
        const src = document.documentElement.outerHTML;
        const m = src.match(/_cf_clientid[=:]["']?([A-F0-9]{32})/i);
        return m ? m[1] : null;
      }) || undefined;
    }
    console.log(`  _cf_clientid: ${cfClientId || "NONE"}`);

    // Nuclear popup removal
    await page.waitForTimeout(3000);
    await page.evaluate(() => {
      document.querySelectorAll('div.resizable.ui-draggable, div[id*="MyServiceWin"], div[id*="MyPrintWin"]').forEach(el => el.remove());
      document.querySelectorAll(".modal-backdrop, .x-mask").forEach(el => el.remove());
    });

    // ===== STEP 1: FETCH TASK LIST FOR ROBYN VIA DIRECT HTTP =====
    console.log("\n--- STEP 1: FETCH TASK LIST (direct HTTP) ---");

    // First navigate to customers page to establish context (ION may need this)
    await page.evaluate(() => {
      // @ts-ignore
      ColdFusionNavigate("/customers/customers.cfm", "pageContent");
    });
    await page.waitForTimeout(2000);

    // Load Robyn's customer tab
    await page.evaluate((custId: string) => {
      // @ts-ignore
      ColdFusionNavigate(`/customers/customerTabs.cfm?customerid=${custId}`, "customerInfo");
    }, ROBYN_ID);
    await page.waitForTimeout(3000);
    console.log("  loaded customer tabs");

    // Now fetch the task list endpoint directly
    const taskListUrl = `${ionOrigin}/tasks/taskList.cfm`;
    console.log(`  fetching: ${taskListUrl}`);

    const taskListHtml = await page.evaluate(async (url: string) => {
      const res = await fetch(url, { credentials: "include", headers: { "Accept": "text/html, */*" } });
      return { ok: res.ok, status: res.status, body: await res.text() };
    }, taskListUrl);

    console.log(`  status: ${taskListHtml.status}, length: ${taskListHtml.body.length}`);
    await Bun.write("./shared/task_list_raw.html", taskListHtml.body);
    console.log("  saved task_list_raw.html");

    // Parse the task list
    const taskRoot = parse(taskListHtml.body);
    
    // Find all links
    const allLinks = taskRoot.querySelectorAll("a");
    console.log(`  total links: ${allLinks.length}`);
    for (const link of allLinks) {
      const href = link.getAttribute("href") || "";
      const onclick = link.getAttribute("onclick") || "";
      const text = link.text.trim().substring(0, 80);
      if (href.includes("task") || href.includes("Task") || onclick.includes("task") || onclick.includes("Task") || text.length > 0) {
        console.log(`  link: text="${text}" href="${href.substring(0, 200)}" onclick="${onclick.substring(0, 200)}"`);
      }
    }

    // Find all tables and dump structure
    const tables = taskRoot.querySelectorAll("table");
    console.log(`  tables: ${tables.length}`);
    for (let i = 0; i < tables.length; i++) {
      const rows = tables[i].querySelectorAll("tr");
      console.log(`  table[${i}]: ${rows.length} rows`);
      for (let j = 0; j < Math.min(rows.length, 8); j++) {
        const cells = rows[j].querySelectorAll("td, th");
        const cellTexts = Array.from(cells).map((c: any) => c.text.trim().substring(0, 40));
        // Also check for links in this row
        const rowLinks = rows[j].querySelectorAll("a");
        const linkInfo = Array.from(rowLinks).map((a: any) => ({
          text: a.text.trim().substring(0, 30),
          href: (a.getAttribute("href") || "").substring(0, 150),
          onclick: (a.getAttribute("onclick") || "").substring(0, 150),
        }));
        console.log(`    row[${j}]: ${JSON.stringify(cellTexts)}`);
        if (linkInfo.length > 0) console.log(`    links: ${JSON.stringify(linkInfo)}`);
      }
    }

    // ===== STEP 2: CLICK TASKS TAB VIA UI (in case direct fetch needs context) =====
    console.log("\n--- STEP 2: CLICK TASKS TAB ---");
    try {
      // Use loadExternalContent which is what the tab onclick does
      await page.evaluate(() => {
        // @ts-ignore
        loadExternalContent("#csttasks", "/tasks/taskList.cfm");
      });
      await page.waitForTimeout(3000);
      await page.screenshot({ path: "./shared/01_tasks_tab.png", fullPage: true });
      console.log("  tasks tab loaded via loadExternalContent");

      // Now check what's visible in the tasks area
      const taskArea = await page.evaluate(() => {
        const area = document.querySelector("#csttasks");
        if (!area) return { found: false, html: "" };
        return {
          found: true,
          html: area.innerHTML.substring(0, 5000),
          links: Array.from(area.querySelectorAll("a")).map((a: any) => ({
            text: a.textContent?.trim().substring(0, 60),
            href: (a.getAttribute("href") || "").substring(0, 200),
            onclick: (a.getAttribute("onclick") || "").substring(0, 200),
          })),
          tables: Array.from(area.querySelectorAll("table")).map((t: any) => ({
            rows: t.querySelectorAll("tr").length,
          })),
        };
      });
      console.log(`  #csttasks found: ${taskArea.found}`);
      if (taskArea.found) {
        console.log(`  links in task area: ${JSON.stringify(taskArea.links, null, 2)}`);
        console.log(`  tables in task area: ${JSON.stringify(taskArea.tables)}`);
        // Save the raw HTML
        await Bun.write("./shared/tasks_area.html", taskArea.html);
      }

      // ===== STEP 3: FIND AND CLICK THE PERPETUAL TASK =====
      console.log("\n--- STEP 3: FIND PERPETUAL TASK ---");
      
      // Look for task links - find the one with "perpetual"
      const perpetualTask = await page.evaluate(() => {
        const area = document.querySelector("#csttasks") || document;
        const rows = area.querySelectorAll("tr");
        const results: any[] = [];
        for (const row of rows) {
          const text = row.textContent?.toLowerCase() || "";
          if (text.includes("perpetual") || text.includes("active")) {
            const cells = row.querySelectorAll("td, th");
            const cellTexts = Array.from(cells).map((c: any) => c.textContent?.trim().substring(0, 60));
            const links = row.querySelectorAll("a");
            const linkInfo = Array.from(links).map((a: any) => ({
              text: a.textContent?.trim().substring(0, 40),
              href: (a.getAttribute("href") || "").substring(0, 250),
              onclick: (a.getAttribute("onclick") || "").substring(0, 250),
            }));
            results.push({ cells: cellTexts, links: linkInfo });
          }
        }
        return results;
      });
      console.log(`  perpetual task matches: ${JSON.stringify(perpetualTask, null, 2)}`);

      // If no perpetual match, just dump all task rows
      if (perpetualTask.length === 0) {
        console.log("  no perpetual match, dumping all task rows:");
        const allTaskRows = await page.evaluate(() => {
          const area = document.querySelector("#csttasks") || document;
          const rows = area.querySelectorAll("tr");
          return Array.from(rows).slice(0, 20).map((row: any) => {
            const cells = row.querySelectorAll("td, th");
            const links = row.querySelectorAll("a");
            return {
              cells: Array.from(cells).map((c: any) => c.textContent?.trim().substring(0, 50)),
              links: Array.from(links).map((a: any) => ({
                text: a.textContent?.trim().substring(0, 30),
                href: (a.getAttribute("href") || "").substring(0, 200),
                onclick: (a.getAttribute("onclick") || "").substring(0, 200),
              })),
            };
          });
        });
        for (const row of allTaskRows) {
          if (row.cells.length > 0) console.log(`  row: ${JSON.stringify(row)}`);
        }
      }

      // ===== STEP 4: CLICK INTO A TASK =====
      console.log("\n--- STEP 4: CLICK INTO TASK ---");
      
      // Try to find and click any task edit link
      const taskEditLink = await page.evaluate(() => {
        const area = document.querySelector("#csttasks") || document;
        const links = area.querySelectorAll("a");
        for (const a of links) {
          const href = a.getAttribute("href") || "";
          const onclick = a.getAttribute("onclick") || "";
          const text = a.textContent?.trim() || "";
          // Look for task detail/edit links
          if (href.includes("taskDetail") || href.includes("taskEdit") || 
              href.includes("addTask") || onclick.includes("taskDetail") || 
              onclick.includes("taskEdit") || onclick.includes("addTask") ||
              href.includes("task") && href.includes(".cfm") && !href.includes("taskList")) {
            return { text, href: href.substring(0, 300), onclick: onclick.substring(0, 300) };
          }
        }
        // Fallback: look for any clickable row or icon that leads to a task
        for (const a of links) {
          const href = a.getAttribute("href") || "";
          const onclick = a.getAttribute("onclick") || "";
          if ((href.includes("Task") || onclick.includes("Task")) && !href.includes("Batch")) {
            return { text: a.textContent?.trim().substring(0, 40), href: href.substring(0, 300), onclick: onclick.substring(0, 300) };
          }
        }
        return null;
      });
      console.log(`  task edit link: ${JSON.stringify(taskEditLink)}`);

      if (taskEditLink) {
        // If it's a ColdFusionNavigate href, extract and call it
        if (taskEditLink.href.includes("ColdFusionNavigate") || taskEditLink.onclick.includes("ColdFusionNavigate")) {
          const navMatch = (taskEditLink.href + taskEditLink.onclick).match(/ColdFusionNavigate\(['"]([^'"]+)['"],\s*['"]([^'"]+)['"]/);
          if (navMatch) {
            console.log(`  navigating to: ${navMatch[1]} in container: ${navMatch[2]}`);
            await page.evaluate(({ path, container }: any) => {
              // @ts-ignore
              ColdFusionNavigate(path, container);
            }, { path: navMatch[1], container: navMatch[2] });
            await page.waitForTimeout(4000);
            await page.screenshot({ path: "./shared/02_task_detail.png", fullPage: true });
          }
        } else {
          // Try clicking the link directly
          try {
            await page.locator(`a[href*="task" i]:not([href*="taskList"]):not([href*="Batch"])`).first().click({ force: true, timeout: 5000 });
            await page.waitForTimeout(4000);
            await page.screenshot({ path: "./shared/02_task_detail.png", fullPage: true });
          } catch (e: any) {
            console.log(`  click failed: ${e.message}`);
          }
        }

        // ===== STEP 5: MAP TASK EDIT FORM =====
        console.log("\n--- STEP 5: MAP TASK EDIT FORM ---");
        
        // Check for new windows that may have opened (ION opens task edits in ServiceWin)
        const formData = await page.evaluate(() => {
          // Search in ServiceWin iframes and main document
          const containers = [
            document.querySelector("#csttasks"),
            document.querySelector('[id*="ServiceWin"] iframe')?.contentDocument,
            document,
          ].filter(Boolean);
          
          const allInputs: any[] = [];
          const allButtons: any[] = [];
          const allForms: any[] = [];
          
          for (const container of containers) {
            if (!container) continue;
            
            // Forms
            const forms = (container as Document).querySelectorAll("form");
            for (const form of forms) {
              allForms.push({
                action: form.action?.substring(0, 200) || "",
                method: form.method,
                id: form.id,
                name: form.name || "",
              });
            }
            
            // Inputs
            const inputs = (container as Document).querySelectorAll("input, select, textarea");
            for (const el of inputs) {
              const inp = el as HTMLInputElement;
              if (inp.type === "hidden" && !inp.name?.toLowerCase().includes("task")) continue;
              allInputs.push({
                tag: el.tagName,
                type: inp.type || "",
                id: inp.id,
                name: inp.name || "",
                value: inp.value?.substring(0, 100) || "",
                visible: inp.offsetWidth > 0,
                options: el.tagName === "SELECT"
                  ? Array.from((el as HTMLSelectElement).options).slice(0, 15).map((o: any) => ({ v: o.value, t: o.text?.substring(0, 50) }))
                  : undefined,
              });
            }
            
            // Buttons
            const btns = (container as Document).querySelectorAll('input[type="submit"], input[type="button"], button, .ovalbutton');
            for (const btn of btns) {
              const text = ((btn as HTMLElement).textContent?.trim() || (btn as HTMLInputElement).value || "").substring(0, 50);
              allButtons.push({
                tag: btn.tagName,
                text,
                id: (btn as HTMLElement).id,
                type: (btn as HTMLInputElement).type || "",
                onclick: btn.getAttribute("onclick")?.substring(0, 200) || "",
                href: btn.getAttribute("href")?.substring(0, 200) || "",
              });
            }
          }
          
          return { forms: allForms, inputs: allInputs.slice(0, 40), buttons: allButtons };
        });
        
        console.log(`  forms: ${JSON.stringify(formData.forms, null, 2)}`);
        console.log(`  inputs (task-related):`);
        for (const inp of formData.inputs) {
          if (inp.visible || inp.name?.toLowerCase().includes("task")) {
            console.log(`    ${inp.tag} name="${inp.name}" id="${inp.id}" type="${inp.type}" value="${inp.value}"`);
            if (inp.options) console.log(`      options: ${JSON.stringify(inp.options)}`);
          }
        }
        console.log(`  buttons: ${JSON.stringify(formData.buttons, null, 2)}`);
      }
    } catch (e: any) {
      console.log(`  UI navigation error: ${e.message}`);
      await page.screenshot({ path: "./shared/error_step.png", fullPage: true });
    }

    // ===== FINAL DUMP =====
    console.log("\n========================================");
    console.log(`TOTAL CFM REQUESTS: ${cfmRequests.length}`);
    console.log("========================================");
    for (const r of cfmRequests) {
      console.log(`${r.method} ${r.url}`);
      if (r.postData) console.log(`  BODY: ${r.postData}`);
    }

    return { cfm_requests: cfmRequests, total: cfmRequests.length };

  } catch (error: any) {
    console.log(`\nFATAL: ${error.message}`);
    console.log(error.stack);
    try { await page.screenshot({ path: "./shared/error.png", fullPage: true }); } catch {}
    throw error;
  } finally {
    await browser.close();
    console.log("browser closed");
  }
}
