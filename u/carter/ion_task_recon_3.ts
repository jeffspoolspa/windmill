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
  const PERPETUAL_EVENT_ID = "1553095";

  console.log("========================================");
  console.log("ION RECON 3: Task Edit Form Deep Dive");
  console.log(`EventID: ${PERPETUAL_EVENT_ID}`);
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

  // Capture _cf_clientid
  let cfClientId: string | undefined;
  page.on("request", (req: any) => {
    const url = req.url();
    if (url.includes("_cf_clientid=") && !cfClientId) {
      const match = url.match(/_cf_clientid=([A-F0-9]{32})/i);
      if (match) cfClientId = match[1];
    }
  });

  // Capture ALL POST requests (these are the write endpoints)
  const postRequests: any[] = [];
  page.on("request", (req: any) => {
    if (req.method() === "POST" && req.url().includes(".cfm")) {
      postRequests.push({
        url: req.url(),
        body: req.postData()?.substring(0, 3000),
        contentType: req.headers()?.["content-type"],
      });
    }
  });

  try {
    // ===== LOGIN + ION =====
    console.log("\n--- LOGIN + ION ---");
    await page.goto(LOGIN_URL);
    await page.locator("#txtUserName").fill(USERNAME);
    await page.locator("#txtPassword").fill(PASSWORD);
    await page.locator('button:has-text("Log In")').click();
    await page.waitForLoadState("networkidle", { timeout: 30000 });

    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 });
    await page.waitForTimeout(1000);
    await page.locator("text=ION POOL CARE").click({ timeout: 5000 });
    await page.waitForLoadState("networkidle", { timeout: 45000 });
    const ionOrigin = new URL(page.url()).origin;
    console.log(`  ION: ${ionOrigin}, _cf_clientid: ${cfClientId}`);

    // Nuclear popup removal
    await page.waitForTimeout(3000);
    await page.evaluate(() => {
      document.querySelectorAll('div.resizable.ui-draggable, div[id*="MyServiceWin"], div[id*="MyPrintWin"]').forEach(el => el.remove());
    });

    // ===== STEP 1: Load customer context first =====
    console.log("\n--- STEP 1: LOAD CUSTOMER CONTEXT ---");
    await page.evaluate(() => {
      // @ts-ignore
      ColdFusionNavigate("/customers/customers.cfm", "pageContent");
    });
    await page.waitForTimeout(2000);

    // Load Robyn's tab
    await page.evaluate((id: string) => {
      // @ts-ignore
      ColdFusionNavigate(`/customers/customerTabs.cfm?customerid=${id}`, "customerInfo");
    }, ROBYN_ID);
    await page.waitForTimeout(3000);
    console.log("  customer context loaded");

    // ===== STEP 2: FETCH TASK EDIT FORM VIA DIRECT HTTP =====
    console.log("\n--- STEP 2: FETCH TASK EDIT FORM ---");
    const taskEditUrl = `${ionOrigin}/tasks/addTask.cfm?EventID=${PERPETUAL_EVENT_ID}`;
    console.log(`  URL: ${taskEditUrl}`);

    const editResult = await page.evaluate(async (url: string) => {
      const res = await fetch(url, { credentials: "include", headers: { "Accept": "text/html, */*" } });
      return { ok: res.ok, status: res.status, body: await res.text() };
    }, taskEditUrl);

    console.log(`  status: ${editResult.status}, length: ${editResult.body.length}`);
    await Bun.write("./shared/task_edit_form.html", editResult.body);
    console.log("  saved task_edit_form.html");

    if (!editResult.ok) {
      console.log(`  response preview: ${editResult.body.substring(0, 1000)}`);
      throw new Error(`Task edit form returned HTTP ${editResult.status}`);
    }

    // ===== STEP 3: PARSE THE FORM =====
    console.log("\n--- STEP 3: PARSE FORM ---");
    const root = parse(editResult.body);

    // Find all <form> tags
    const forms = root.querySelectorAll("form");
    console.log(`  forms found: ${forms.length}`);
    for (const form of forms) {
      const action = form.getAttribute("action") || "";
      const method = form.getAttribute("method") || "";
      const id = form.getAttribute("id") || "";
      const name = form.getAttribute("name") || "";
      console.log(`  form: id="${id}" name="${name}" method="${method}" action="${action}"`);
    }

    // Find ALL input, select, textarea elements
    console.log("\n  --- FORM FIELDS ---");
    const inputs = root.querySelectorAll("input, select, textarea");
    for (const el of inputs) {
      const tag = el.tagName;
      const type = el.getAttribute("type") || "";
      const name = el.getAttribute("name") || "";
      const id = el.getAttribute("id") || "";
      const value = el.getAttribute("value")?.substring(0, 100) || "";
      const checked = el.getAttribute("checked") !== null ? " CHECKED" : "";

      // For selects, get all options
      if (tag === "SELECT") {
        const options = el.querySelectorAll("option");
        const optList = options.map((o: any) => {
          const selected = o.getAttribute("selected") !== null ? " *SELECTED*" : "";
          return `${o.getAttribute("value") || ""}="${o.text.trim().substring(0, 50)}"${selected}`;
        });
        console.log(`  SELECT name="${name}" id="${id}"`);
        for (const opt of optList) {
          console.log(`    option: ${opt}`);
        }
      } else if (tag === "TEXTAREA") {
        const text = el.text.trim().substring(0, 200);
        console.log(`  TEXTAREA name="${name}" id="${id}" value="${text}"`);
      } else {
        console.log(`  ${tag} type="${type}" name="${name}" id="${id}" value="${value}"${checked}`);
      }
    }

    // Find buttons/submit elements
    console.log("\n  --- BUTTONS ---");
    const buttons = root.querySelectorAll('input[type="submit"], input[type="button"], button, a.ovalbutton, .ovalbutton');
    for (const btn of buttons) {
      const text = btn.text.trim().substring(0, 50) || btn.getAttribute("value") || "";
      const onclick = btn.getAttribute("onclick")?.substring(0, 200) || "";
      const href = btn.getAttribute("href")?.substring(0, 200) || "";
      const type = btn.getAttribute("type") || "";
      console.log(`  BUTTON text="${text}" type="${type}" onclick="${onclick}" href="${href}"`);
    }

    // Find any JavaScript that handles form submission
    console.log("\n  --- SCRIPTS (task-related) ---");
    const scripts = root.querySelectorAll("script");
    for (const script of scripts) {
      const content = script.text;
      if (content.includes("task") || content.includes("Task") || content.includes("save") || content.includes("Save") || content.includes("submit") || content.includes("Submit") || content.includes("addTask") || content.includes("updateTask")) {
        // Print relevant snippets
        const lines = content.split("\n");
        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.length > 5 && (
            trimmed.includes("task") || trimmed.includes("Task") ||
            trimmed.includes("save") || trimmed.includes("Save") ||
            trimmed.includes("submit") || trimmed.includes("Submit") ||
            trimmed.includes("ajax") || trimmed.includes("Ajax") ||
            trimmed.includes("$.post") || trimmed.includes("$.get") ||
            trimmed.includes("fetch") || trimmed.includes("action") ||
            trimmed.includes("form") || trimmed.includes("Form") ||
            trimmed.includes("url:") || trimmed.includes("method:")
          )) {
            console.log(`    ${trimmed.substring(0, 200)}`);
          }
        }
      }
    }

    // ===== STEP 4: Also check what happens via the UI route =====
    // Load the task in the ServiceWin iframe (the way ION normally does it)
    console.log("\n--- STEP 4: LOAD TASK VIA UI (ServiceWin iframe) ---");
    
    // First, we need to load the task list tab
    await page.evaluate(() => {
      // @ts-ignore
      loadExternalContent("#csttasks", "/tasks/taskList.cfm");
    });
    await page.waitForTimeout(3000);

    // Now we need to handle the ServiceWin3 popup for the task edit
    // ION opens tasks in an iframe inside a floating window
    // Let's try to create the window and load the task into it
    const taskFrameUrl = `${ionOrigin}/tasks/addTask.cfm?EventID=${PERPETUAL_EVENT_ID}`;
    
    // Try loading it via page navigation to a new page for form inspection
    const taskPage = await context.newPage();
    
    // Copy cookies from main page context
    await taskPage.goto(taskFrameUrl);
    await taskPage.waitForLoadState("networkidle", { timeout: 15000 });
    
    console.log(`  task page URL: ${taskPage.url()}`);
    console.log(`  task page title: ${await taskPage.title()}`);
    await taskPage.screenshot({ path: "./shared/task_edit_page.png", fullPage: true });
    console.log("  screenshot: task_edit_page.png");

    // Map form in the new page context
    const pageFormData = await taskPage.evaluate(() => {
      const forms = document.querySelectorAll("form");
      const formInfo = Array.from(forms).map((f: any) => ({
        id: f.id, name: f.name, action: f.action, method: f.method,
        onsubmit: f.getAttribute("onsubmit")?.substring(0, 200),
      }));

      const inputs = document.querySelectorAll("input, select, textarea");
      const fieldInfo = Array.from(inputs).map((el: any) => {
        const base: any = {
          tag: el.tagName, type: el.type || "", name: el.name || "", id: el.id || "",
          value: el.value?.substring(0, 150) || "",
          visible: el.offsetWidth > 0 && el.offsetHeight > 0,
        };
        if (el.tagName === "SELECT") {
          base.selectedIndex = el.selectedIndex;
          base.options = Array.from(el.options).map((o: any) => ({
            v: o.value, t: o.text?.trim().substring(0, 50),
            selected: o.selected,
          }));
        }
        if (el.type === "checkbox" || el.type === "radio") {
          base.checked = el.checked;
        }
        return base;
      });

      const buttons = document.querySelectorAll('input[type="submit"], input[type="button"], button, .ovalbutton');
      const btnInfo = Array.from(buttons).map((b: any) => ({
        tag: b.tagName, text: (b.textContent?.trim() || b.value || "").substring(0, 60),
        id: b.id, type: b.type || "",
        onclick: b.getAttribute("onclick")?.substring(0, 300) || "",
      }));

      return { forms: formInfo, fields: fieldInfo, buttons: btnInfo };
    });

    console.log(`\n  --- PAGE FORM DATA ---`);
    console.log(`  forms: ${JSON.stringify(pageFormData.forms, null, 2)}`);
    console.log(`  visible fields:`);
    for (const f of pageFormData.fields) {
      if (f.visible || f.name?.toLowerCase().includes("task") || f.name?.toLowerCase().includes("event")) {
        console.log(`    ${f.tag} name="${f.name}" id="${f.id}" type="${f.type}" value="${f.value}"`);
        if (f.options) {
          for (const o of f.options) {
            console.log(`      ${o.selected ? "→" : " "} ${o.v}="${o.t}"`);
          }
        }
      }
    }
    console.log(`  hidden fields:`);
    for (const f of pageFormData.fields) {
      if (!f.visible && (f.type === "hidden" || f.name)) {
        console.log(`    ${f.tag} name="${f.name}" type="${f.type}" value="${f.value}"`);
      }
    }
    console.log(`  buttons: ${JSON.stringify(pageFormData.buttons, null, 2)}`);

    await taskPage.close();

    // ===== DUMP POST REQUESTS =====
    console.log("\n========================================");
    console.log("POST REQUESTS CAPTURED:");
    console.log("========================================");
    for (const r of postRequests) {
      console.log(`POST ${r.url}`);
      if (r.body) console.log(`  body: ${r.body}`);
    }

    return { success: true, post_requests: postRequests };

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
