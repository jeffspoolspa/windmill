//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0";
import * as wmill from "windmill-client";
import { mkdir } from "fs/promises";

export async function main() {
  const LOGIN_URL = await wmill.getVariable("f/ION/LOGIN_URL");
  const USERNAME = await wmill.getVariable("f/ION/USERNAME");
  const PASSWORD = await wmill.getVariable("f/ION/PASSWORD");
  const CUSTOMER_NAME = "cheek";

  console.log("========================================");
  console.log("ION RECON: Customer Tasks Endpoints");
  console.log(`Customer search: ${CUSTOMER_NAME}`);
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

  // Capture all ColdFusion requests
  const cfmRequests: any[] = [];
  page.on("request", (req: any) => {
    const url = req.url();
    const method = req.method();
    if (url.includes(".cfm")) {
      const entry: any = { method, url: url.substring(0, 500) };
      if (method === "POST") {
        entry.postData = req.postData()?.substring(0, 2000) || null;
        entry.contentType = req.headers()?.["content-type"] || null;
      }
      cfmRequests.push(entry);
      console.log(`  [${method}] ${url.substring(0, 200)}`);
      if (entry.postData) console.log(`    body: ${entry.postData.substring(0, 500)}`);
    }
  });

  try {
    // ===== LOGIN + ION REDIRECT =====
    console.log("\n--- LOGIN ---");
    await page.goto(LOGIN_URL);
    await page.locator("#txtUserName").fill(USERNAME);
    await page.locator("#txtPassword").fill(PASSWORD);
    await page.locator('button:has-text("Log In")').click();
    await page.waitForLoadState("networkidle", { timeout: 30000 });

    console.log("\n--- ION REDIRECT ---");
    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 });
    await page.waitForTimeout(1000);
    await page.locator("text=ION POOL CARE").click({ timeout: 5000 });
    await page.waitForLoadState("networkidle", { timeout: 45000 });
    console.log(`  at: ${page.url()}`);

    // Nuclear popup removal
    await page.waitForTimeout(3000);
    await page.evaluate(() => {
      document.querySelectorAll('div.resizable.ui-draggable, div[id*="MyServiceWin"], div[id*="MyPrintWin"]').forEach(el => el.remove());
      document.querySelectorAll(".modal-backdrop, .x-mask").forEach(el => el.remove());
    });
    console.log("  popups cleared");

    // ===== CUSTOMERS PAGE =====
    console.log("\n--- CUSTOMERS ---");
    await page.evaluate(() => {
      // @ts-ignore
      ColdFusionNavigate("/customers/customers.cfm", "pageContent");
    });
    await page.waitForTimeout(4000);
    await page.screenshot({ path: "./shared/01_customers.png", fullPage: true });

    // Map all form elements on the page
    const pageInputs = await page.evaluate(() => {
      const els = document.querySelectorAll("input, select, textarea, button, .ovalbutton");
      return Array.from(els).slice(0, 30).map((el: any) => ({
        tag: el.tagName, type: el.type || "", id: el.id, name: el.name || "",
        value: el.value?.substring(0, 50) || "",
        text: el.textContent?.trim().substring(0, 40) || "",
        placeholder: el.placeholder || "",
        visible: el.offsetWidth > 0,
      }));
    });
    console.log(`  page elements:\n${JSON.stringify(pageInputs, null, 2)}`);

    // ===== SEARCH =====
    console.log("\n--- SEARCH ---");
    // Try the most common ION customer search pattern
    let searched = false;

    // Check for last name input specifically
    for (const sel of ['input#custLastName', 'input[name="custLastName"]', 'input[name="LastName"]', 'input[name="lastName"]']) {
      try {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 500 })) {
          await el.fill(CUSTOMER_NAME);
          console.log(`  filled ${sel} with "${CUSTOMER_NAME}"`);
          searched = true;
          break;
        }
      } catch {}
    }

    // Fallback: try first visible text input
    if (!searched) {
      const firstInput = await page.evaluate(() => {
        const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
        for (const inp of inputs) {
          const rect = (inp as HTMLElement).getBoundingClientRect();
          if (rect.width > 50 && rect.height > 10) {
            return { id: (inp as HTMLElement).id, name: (inp as HTMLInputElement).name };
          }
        }
        return null;
      });
      if (firstInput) {
        const sel = firstInput.id ? `#${firstInput.id}` : `input[name="${firstInput.name}"]`;
        await page.locator(sel).fill(CUSTOMER_NAME);
        console.log(`  filled fallback input: ${sel}`);
        searched = true;
      }
    }

    if (searched) {
      // Try submit button or Enter
      let clicked = false;
      for (const sel of ['.ovalbutton:has-text("Search")', '.ovalbutton:has-text("Go")', 'input[type="submit"]', 'button:has-text("Search")', 'button:has-text("Go")']) {
        try {
          const el = page.locator(sel).first();
          if (await el.isVisible({ timeout: 500 })) {
            await el.click({ force: true });
            clicked = true;
            console.log(`  clicked: ${sel}`);
            break;
          }
        } catch {}
      }
      if (!clicked) {
        await page.keyboard.press("Enter");
        console.log("  pressed Enter");
      }
    }

    await page.waitForTimeout(4000);
    await page.screenshot({ path: "./shared/02_search.png", fullPage: true });

    // ===== CLICK CUSTOMER =====
    console.log("\n--- CUSTOMER RESULT ---");
    const custLinks = await page.evaluate(() => {
      const links = document.querySelectorAll("a");
      return Array.from(links).filter((a: any) => {
        const text = a.textContent?.toLowerCase() || "";
        const href = (a.getAttribute("href") || "").toLowerCase();
        const onclick = (a.getAttribute("onclick") || "").toLowerCase();
        return text.includes("cheek") || (href.includes("custdetail") || onclick.includes("custdetail"));
      }).slice(0, 5).map((a: any) => ({
        text: a.textContent?.trim().substring(0, 60),
        href: (a.getAttribute("href") || "").substring(0, 200),
        onclick: (a.getAttribute("onclick") || "").substring(0, 200),
      }));
    });
    console.log(`  customer links: ${JSON.stringify(custLinks, null, 2)}`);

    if (custLinks.length > 0) {
      // Click first match
      await page.locator('a:has-text("CHEEK"), a:has-text("Cheek")').first().click({ timeout: 5000, force: true });
      console.log("  clicked customer");
      await page.waitForTimeout(4000);
      await page.screenshot({ path: "./shared/03_customer.png", fullPage: true });

      // ===== TASKS TAB =====
      console.log("\n--- TASKS TAB ---");
      // Find all tab-like elements
      const allTabs = await page.evaluate(() => {
        const els = document.querySelectorAll("a, button, .ovalbutton, .tab-link, [class*=tab]");
        return Array.from(els).filter((el: any) => {
          const text = el.textContent?.trim().toLowerCase() || "";
          return text.length < 30 && (text.includes("task") || text.includes("equip") || text.includes("service") || text.includes("note") || text.includes("bill") || text.includes("contact") || text.includes("detail") || text.includes("history"));
        }).map((el: any) => ({
          text: el.textContent?.trim().substring(0, 30),
          tag: el.tagName, id: el.id,
          href: (el.getAttribute("href") || "").substring(0, 150),
          onclick: (el.getAttribute("onclick") || "").substring(0, 150),
          class: el.className?.substring?.(0, 50) || "",
        }));
      });
      console.log(`  tabs found: ${JSON.stringify(allTabs, null, 2)}`);

      // Click Tasks
      let foundTasks = false;
      for (const sel of ['a:has-text("Tasks")', '.ovalbutton:has-text("Tasks")', 'a:has-text("Task")', 'button:has-text("Task")']) {
        try {
          const el = page.locator(sel).first();
          if (await el.isVisible({ timeout: 500 })) {
            await el.click({ force: true });
            foundTasks = true;
            console.log(`  clicked: ${sel}`);
            break;
          }
        } catch {}
      }

      if (foundTasks) {
        await page.waitForTimeout(4000);
        await page.screenshot({ path: "./shared/04_tasks.png", fullPage: true });

        // ===== MAP TASK LIST =====
        console.log("\n--- TASK LIST ---");
        const taskData = await page.evaluate(() => {
          const links = document.querySelectorAll("a");
          const taskLinks: any[] = [];
          for (const a of links) {
            const href = a.getAttribute("href") || "";
            const onclick = a.getAttribute("onclick") || "";
            const text = a.textContent?.trim() || "";
            if (href.includes("task") || href.includes("Task") || onclick.includes("task") || onclick.includes("Task")) {
              taskLinks.push({ text: text.substring(0, 80), href: href.substring(0, 250), onclick: onclick.substring(0, 250) });
            }
          }
          // Also grab table data
          const tables = document.querySelectorAll("table");
          const tableSummary = Array.from(tables).map((t: any) => ({
            rows: t.querySelectorAll("tr").length,
            firstRow: t.querySelector("tr")?.textContent?.trim().substring(0, 100),
          }));
          return { taskLinks: taskLinks.slice(0, 10), tables: tableSummary };
        });
        console.log(`  task data: ${JSON.stringify(taskData, null, 2)}`);

        // Click into first task
        if (taskData.taskLinks.length > 0) {
          console.log("\n--- TASK DETAIL ---");
          try {
            await page.locator('a[href*="task" i], a[onclick*="task" i]').first().click({ timeout: 5000, force: true });
            await page.waitForTimeout(4000);
            await page.screenshot({ path: "./shared/05_task_detail.png", fullPage: true });

            // Map the edit form
            const formMap = await page.evaluate(() => {
              const els = document.querySelectorAll("input, select, textarea");
              return Array.from(els).map((el: any) => ({
                tag: el.tagName, type: el.type || "", id: el.id, name: el.name || "",
                value: el.value?.substring(0, 100) || "",
                visible: el.offsetWidth > 0,
                options: el.tagName === "SELECT"
                  ? Array.from(el.options).slice(0, 10).map((o: any) => ({ v: o.value, t: o.text?.substring(0, 40) }))
                  : undefined,
              }));
            });
            console.log(`  form elements: ${JSON.stringify(formMap, null, 2)}`);

            // Find save/action buttons
            const buttons = await page.evaluate(() => {
              const btns = document.querySelectorAll('input[type="submit"], input[type="button"], button, .ovalbutton, a.btn');
              return Array.from(btns).map((el: any) => ({
                tag: el.tagName, text: (el.textContent?.trim() || el.value || "").substring(0, 50),
                id: el.id, type: el.type || "",
                onclick: (el.getAttribute("onclick") || "").substring(0, 200),
                href: (el.getAttribute("href") || "").substring(0, 150),
              }));
            });
            console.log(`  buttons: ${JSON.stringify(buttons, null, 2)}`);

          } catch (e: any) {
            console.log(`  task detail failed: ${e.message}`);
          }
        }
      } else {
        console.log("  Tasks tab not found");
      }
    } else {
      console.log("  No customer links found");
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
    try { await page.screenshot({ path: "./shared/error.png", fullPage: true }); } catch {}
    console.log("\nCFM REQUESTS BEFORE FAILURE:");
    for (const r of cfmRequests) {
      console.log(`${r.method} ${r.url}`);
      if (r.postData) console.log(`  BODY: ${r.postData}`);
    }
    throw error;
  } finally {
    await browser.close();
    console.log("browser closed");
  }
}
