
def main():
    # This script returns raw HTML for the landing page
    # Call via webhook: GET /api/w/jps-internal/jobs/run_wait_result/p/u/carter/switch_to_weekly_page
    # The landing page reads query params client-side via JavaScript
    
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Switch to Weekly Service</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f8f9fa;color:#333;line-height:1.6}
.header{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);color:#fff;padding:2rem 1rem;text-align:center}
.header h1{font-size:1.8rem;margin-bottom:.5rem;font-weight:600}
.header p{font-size:.95rem;opacity:.85}
.container{max-width:600px;margin:0 auto;padding:2rem 1rem}
.card{background:#fff;border-radius:8px;padding:2rem;box-shadow:0 2px 12px rgba(0,0,0,.08);margin-bottom:2rem}
.card h2{color:#0f172a;margin-bottom:1rem;font-size:1.4rem}
.card p{margin-bottom:1rem;color:#555}
.details{background:#f8f9fa;border-left:4px solid #3b82f6;padding:1rem;border-radius:4px;margin:1.5rem 0}
.details-row{display:flex;justify-content:space-between;margin-bottom:.75rem}
.details-row:last-child{margin-bottom:0}
.details-label{color:#666;font-weight:500}
.details-value{color:#0f172a;font-weight:600}
.benefits{background:#f0fdf4;border-radius:6px;padding:1.5rem;margin:1.5rem 0;border:1px solid #bbf7d0}
.benefits h3{color:#166534;margin-bottom:1rem;font-size:1rem}
.benefits ul{list-style:none;padding:0}
.benefits li{padding-left:1.5rem;margin-bottom:.5rem;position:relative;color:#555}
.benefits li:before{content:"\2713";position:absolute;left:0;color:#16a34a;font-weight:bold}
.btn{display:block;width:100%;padding:1rem 2rem;border:none;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;transition:all .3s;text-align:center;margin-top:2rem}
.btn-primary{background:linear-gradient(135deg,#3b82f6 0%,#1d4ed8 100%);color:#fff;box-shadow:0 4px 12px rgba(59,130,246,.3)}
.btn-primary:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 6px 16px rgba(59,130,246,.4)}
.btn-primary:disabled{opacity:.6;cursor:not-allowed}
.state{display:none}.state.active{display:block}
.loading{text-align:center;padding:2rem}
.spinner{border:4px solid #f0f0f0;border-top:4px solid #3b82f6;border-radius:50%;width:40px;height:40px;animation:spin 1s linear infinite;margin:0 auto 1rem}
@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
.success-icon{width:64px;height:64px;border-radius:50%;background:#dcfce7;display:flex;align-items:center;justify-content:center;margin:0 auto 1rem;font-size:2rem;color:#16a34a}
.error{background:#fef3c7;border:1px solid #fbbf24;color:#92400e;padding:1rem;border-radius:6px;margin-bottom:1rem}
.rate-comparison{display:flex;align-items:center;justify-content:center;gap:1rem;margin:1.5rem 0;flex-wrap:wrap}
.rate-old{font-size:1.3rem;color:#9ca3af;text-decoration:line-through;font-weight:600}
.rate-arrow{color:#9ca3af;font-size:1.2rem}
.rate-new{font-size:1.8rem;color:#1e40af;font-weight:700}
.rate-label{font-size:.85rem;color:#6b7280;text-align:center;margin-top:.25rem}
.contact-info{text-align:center;margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e5e7eb;font-size:.9rem;color:#6b7280}
.contact-info a{color:#3b82f6;text-decoration:none}
@media(max-width:480px){.header h1{font-size:1.4rem}.card{padding:1.5rem}.details-row{flex-direction:column}.details-value{margin-top:.25rem}}
</style>
</head>
<body>
<div class="header"><h1 id="companyName">Pool Service</h1><p>Service Confirmation</p></div>
<div class="container">
<div class="state active" id="loadingState"><div class="card"><div class="loading"><div class="spinner"></div><p>Loading your information...</p></div></div></div>
<div class="state" id="errorState"><div class="card"><h2>Something Went Wrong</h2><div class="error"><p id="errorMessage">The confirmation link appears to be incomplete. Please check your email and try the link again.</p></div><p class="contact-info">If this keeps happening, call us at <span id="errorPhone"></span></p></div></div>
<div class="state" id="confirmState"><div class="card">
<h2 id="greeting">Confirm Your Switch to Weekly</h2>
<p>You are about to switch from bi-weekly to <strong>weekly pool service</strong>. Here is a summary of your new rate:</p>
<div class="rate-comparison"><div><div class="rate-old" id="oldRate">$75</div><div class="rate-label">Bi-Weekly</div></div><div class="rate-arrow">&rarr;</div><div><div class="rate-new" id="newRate">$50</div><div class="rate-label">Weekly</div></div></div>
<p style="text-align:center;font-size:.85rem;color:#6b7280">Labor rate per visit &mdash; chemicals billed separately</p>
<div class="benefits"><h3>What You Get with Weekly Service</h3><ul>
<li>Consistently clear water all season</li>
<li>Green-Free Guarantee &mdash; if it turns green, recovery labor is on us</li>
<li>Better chemical balance with less product per visit</li>
<li>Weekly equipment checks catch problems early</li>
</ul></div>
<p style="font-size:.9rem;color:#666"><strong>What happens next:</strong> Click below to confirm and our scheduling team will get you on the next available weekly route. We will reach out within 1&ndash;2 business days. You can also call our office directly.</p>
<button class="btn btn-primary" id="confirmBtn" onclick="submitConfirmation()">Confirm Switch to Weekly</button>
</div></div>
<div class="state" id="processingState"><div class="card"><div class="loading"><div class="spinner"></div><p>Submitting your request...</p></div></div></div>
<div class="state" id="successState"><div class="card">
<div class="success-icon">&#10003;</div>
<h2 style="text-align:center;color:#166534">You Are All Set!</h2>
<p style="text-align:center;font-size:1.05rem;margin-top:1rem">Your request to switch to weekly service has been submitted.</p>
<div class="details" style="border-left-color:#22c55e;margin-top:1.5rem"><div class="details-row"><span class="details-label">Status:</span><span class="details-value" style="color:#16a34a">Confirmed</span></div><div class="details-row"><span class="details-label">New Rate:</span><span class="details-value" id="successRate"></span></div></div>
<p style="margin-top:1.5rem;color:#555;text-align:center">Our scheduling team will review your request and contact you within 1&ndash;2 business days to confirm your new weekly route.</p>
<div class="contact-info" id="successContact"></div>
</div></div>
<div class="state" id="submissionErrorState"><div class="card">
<h2>Something Went Wrong</h2>
<div class="error"><p id="submissionErrorMsg">We had trouble submitting your request. Please try again or call us directly.</p></div>
<button class="btn btn-primary" onclick="showState('confirmState')">Try Again</button>
<div class="contact-info" id="errorContact"></div>
</div></div>
</div>
<script>
var API_URL = "https://vvprodiuwraceabviyes.supabase.co/functions/v1/switch-to-weekly";
var OFFICES = {"Jeff's Pool & Spa": {displayName: "Jeff's Pool & Spa Service", phone: "(912) 554-0636", email: "jpsbilling@jeffspoolspa.com"}, "Perfect Pools": {displayName: "Perfect Pools", phone: "(912) 459-0160", email: "info@perfectpoolscleaning.com"}};
function getQueryParams() { var p = new URLSearchParams(window.location.search); return {name: p.get("name")||"", last: p.get("last")||"", customer: p.get("customer")||"", email: p.get("email")||"", office: p.get("office")||"", rate: p.get("rate")||"", oldrate: p.get("oldrate")||"", phone: p.get("phone")||"", address: p.get("address")||""}; }
function showState(s) { document.querySelectorAll(".state").forEach(function(e){e.classList.remove("active")}); var el=document.getElementById(s); if(el)el.classList.add("active"); }
function formatRate(v) { var n=parseFloat(v); return isNaN(n)?"$"+v:"$"+n.toFixed(0); }
function initPage() {
  var params = getQueryParams();
  if (!params.name||!params.email||!params.office||!params.rate) { showState("errorState"); var oi=OFFICES[params.office]||OFFICES["Jeff's Pool & Spa"]; document.getElementById("errorPhone").textContent=oi.phone; return; }
  window.customerData = params;
  var oi = OFFICES[params.office];
  if (!oi) { showState("errorState"); document.getElementById("errorMessage").textContent="Invalid office in link. Please contact us."; return; }
  document.getElementById("companyName").textContent = oi.displayName;
  document.getElementById("greeting").textContent = "Hi "+params.name+", Confirm Your Switch";
  if (params.oldrate) document.getElementById("oldRate").textContent = formatRate(params.oldrate);
  document.getElementById("newRate").textContent = formatRate(params.rate);
  var ch = 'Questions? Call <a href="tel:'+oi.phone+'">'+oi.phone+'</a> or email <a href="mailto:'+oi.email+'">'+oi.email+'</a>';
  ["successContact","errorContact"].forEach(function(id){ var el=document.getElementById(id); if(el)el.innerHTML=ch; });
  setTimeout(function(){ showState("confirmState"); }, 400);
}
async function submitConfirmation() {
  if (window._submitted) { showState("successState"); return; }
  var btn = document.getElementById("confirmBtn"); btn.disabled = true; showState("processingState");
  var p = window.customerData;
  try {
    var r = await fetch(API_URL, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:p.name,last:p.last,customer:p.customer,email:p.email,office:p.office,rate:p.rate,phone:p.phone,address:p.address}) });
    var d = await r.json();
    if (r.ok && d.success) { window._submitted=true; document.getElementById("successRate").textContent=formatRate(p.rate)+"/visit (plus chemicals)"; showState("successState"); }
    else { throw new Error(d.error||"Failed"); }
  } catch(e) { console.error(e); document.getElementById("submissionErrorMsg").textContent="We had trouble submitting your request. Please try again or call us directly."; showState("submissionErrorState"); btn.disabled=false; }
}
document.addEventListener("DOMContentLoaded", initPage);
</script>
</body>
</html>"""
    
    return html
