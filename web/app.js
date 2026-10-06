/* Android Bay — local-only UI. All device-provided text is rendered as text. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {config:null, devices:[], selected:null, inspection:null, inspecting:false, jobs:[], activeJob:null, busy:new Set(), connected:false, poll:null, pollFailures:0, view:"transfer"};
  const activeStatuses = new Set(["running", "queued", "verifying", "starting", "waiting", "finalizing"]);
  const views = {transfer:"Transfer center", history:"Saved transfers", guide:"Connection guide"};
  const create = (tag, className, text) => {const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = String(text); return element;};
  const str = (value, fallback="") => typeof value === "string" ? value : (value == null ? fallback : String(value));
  const deviceStatus = (device) => str(device.state || device.status).toLowerCase();
  const authorized = (device) => Boolean(device && (deviceStatus(device) === "device" || device.authorized === true));
  const isActive = (job) => Boolean(job && activeStatuses.has(str(job.status).toLowerCase()));
  const humanize = (value) => str(value).replace(/[_-]/g," ").replace(/^\w/,c=>c.toUpperCase());
  const bytes = (value) => {let n = Number(value); if (!Number.isFinite(n) || n < 0) n = 0; const units=["B","KiB","MiB","GiB","TiB"]; let i=0; while(n>=1024 && i<units.length-1){n/=1024;i++;} return `${n.toLocaleString(undefined,{maximumFractionDigits:i?1:0})} ${units[i]}`;};
  const date = (value) => {const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "Date unavailable" : parsed.toLocaleString(undefined,{month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"});};
  const number = (value) => Math.max(0,Number(value)||0).toLocaleString();
  function announce(message){$("live-status").textContent = message;}
  function notice(message,kind="error"){const box=$("notice"); box.className=`notice ${kind}`; $("notice-text").textContent=str(message); box.hidden=false;}
  function clearNotice(){$("notice").hidden=true;}
  async function api(path,body){
    const options={method:body === undefined ? "GET" : "POST",cache:"no-store",credentials:"same-origin",headers:{"Accept":"application/json"}};
    if(body !== undefined){if(!state.config?.csrfToken) throw new Error("The local service is not ready. Refresh this page and try again."); options.headers["Content-Type"]="application/json"; options.headers["X-CSRF-Token"]=state.config.csrfToken; options.body=JSON.stringify(body);}
    let response; try{response=await fetch(path,options);}catch{throw new Error("Cannot reach the local transfer service. Keep Android Bay running, then refresh this page.");}
    let result; try{result=await response.json();}catch{throw new Error("The local service returned an unreadable response. Restart Android Bay and try again.");}
    if(!response.ok) throw new Error(str(result.error || result.message,`Request failed (${response.status}).`));
    return result;
  }
  async function perform(key,button,label,callback){
    if(state.busy.has(key)) return;
    state.busy.add(key); const previous=button?.textContent; if(button){button.disabled=true;if(label)button.textContent=label;} clearNotice();
    try{return await callback();}catch(error){notice(error.message || "The operation could not be completed.");}
    finally{state.busy.delete(key);if(button){button.textContent=previous;button.disabled=false;}updateControls();}
  }
  function switchView(view,{focus=false}={}){
    if(!views[view])return;state.view=view;
    for(const name of Object.keys(views))$("view-"+name).hidden=name!==view;
    for(const item of document.querySelectorAll("[data-view]")){const active=item.dataset.view===view;item.classList.toggle("active",active);if(active)item.setAttribute("aria-current","page");else item.removeAttribute("aria-current");}
    $("view-label").textContent=views[view];
    if(focus)$("main-content").focus({preventScroll:true});
    if(view==="history") refreshHistory().catch(error=>notice(error.message));
  }
  function updateControls(){
    const selected = state.devices.find(device=>device.serial===state.selected); const ready=state.connected && authorized(selected) && Boolean(state.config?.adbAvailable);
    const jobsRunning=state.jobs.some(isActive) || isActive(state.activeJob);
    $("analyze-job").disabled=!state.config?.archiveChecksAvailable || jobsRunning || !state.activeJob?.destination || state.busy.has("job-action");
    const helperExport=state.inspection?.helperExport;
    const exportRunning=helperExport?.state==="running";
    const optionsSelected=["option-shared","option-apks","option-helper","option-legacy","option-private","option-root","option-context"].some(id=>$(id).checked);
    $("extra-roots").disabled=!$("option-shared").checked;
    $("start-transfer").disabled=!ready || !state.inspection || state.inspecting || jobsRunning || exportRunning || !$("destination").value.trim() || !optionsSelected || state.busy.has("start");
    $("install-helper").disabled=!ready || !state.inspection || Number(state.inspection.sdk || 0)<19 || !state.config?.helperAvailable || state.busy.has("helper") || jobsRunning;
    $("open-helper").disabled=!ready || !state.inspection?.helperInstalled || state.busy.has("helper") || jobsRunning;
    $("mirror-phone").disabled=!ready || !state.config?.mirrorAvailable || Number(state.inspection?.sdk || 0)<21 || state.busy.has("mirror");
    $("browse-folder").disabled=!state.connected || state.busy.has("browse");
    $("refresh-devices").disabled=!state.connected || state.busy.has("devices");
    if(jobsRunning){$("start-ready").textContent="A transfer is already in progress";$("start-help").textContent="Keep the phone connected. You can follow the current transfer below.";}
    else if(!ready){$("start-ready").textContent="Connect and authorize a phone to continue";$("start-help").textContent=state.connected?"No account, upload or internet connection is needed for a packaged transfer.":"The local transfer service is unavailable. Keep Android Bay running.";}
    else if(state.inspecting){$("start-ready").textContent="Checking accessible storage…";$("start-help").textContent="Your phone’s permissions determine which data can be copied.";}
    else if(exportRunning){$("start-ready").textContent="Phone export is still running";$("start-help").textContent="Wait for Create export to finish on the phone, then click Refresh above. Copying is paused to avoid reading unfinished exports.";}
    else if(!state.inspection){$("start-ready").textContent="Select your phone to retry the storage check";$("start-help").textContent="A successful inspection is needed before starting a new copy.";}
    else if(!optionsSelected){$("start-ready").textContent="Choose at least one data category";$("start-help").textContent="Select the files or exports you want to include above.";}
    else{$("start-ready").textContent=`Ready to copy from ${str(state.inspection?.model || selected.model || selected.serial).replace(/_/g," ")}`;$("start-help").textContent="Keep the phone connected and unlocked until the transfer finishes.";}
    if(state.inspection && Number(state.inspection.sdk || 0)<19)$("helper-status").textContent="The phone helper requires Android 4.4 or newer. This phone can still use accessible file and APK copying.";
    else if(!state.config?.helperAvailable)$("helper-status").textContent="The helper APK is not included in this package. Files and app installers can still be copied.";
    else if(state.inspection?.helperInstalled)$("helper-status").textContent="Helper is installed. Open it on the phone and finish an export before copying.";
    else $("helper-status").textContent="Installing is optional. Your phone may ask you to allow installation over USB.";
    const exportStatus=$("helper-export-state");
    exportStatus.hidden=!helperExport || !helperExport.state;
    if(helperExport?.state){
      const labels={ready:"Phone export ready",running:"Phone export in progress",missing:"No phone export found",partial:"Phone export needs review",incomplete:"Phone export is incomplete"};
      exportStatus.className=`helper-export-state ${helperExport.state==="ready"?"ready":"warning"}`;
      exportStatus.textContent=`${labels[helperExport.state] || "Phone export status"}. ${str(helperExport.detail,helperExport.state==="running"?"Keep the helper open on your phone until it finishes, then click Refresh.":helperExport.state==="missing"?"Open the helper, tap Grant permissions, then Create export before copying phone records.":"Review the helper’s result before starting your transfer.")}`;
      if(helperExport.state==="incomplete")exportStatus.append(document.createTextNode(" If the phone is still exporting, wait and click Refresh. If it was interrupted, reopen the helper and run Create export again. You can still recover other accessible files now; unfinished phone records remain marked incomplete."));
    }
    const step=isActive(state.activeJob)?3:state.activeJob?4:ready?2:1;
    for(const [index,id] of ["connect","prepare","copy","verify"].entries()){
      const item=$("step-"+id);item.classList.toggle("current",index+1===step);item.classList.toggle("complete",index+1<step);
      if(index+1===step)item.setAttribute("aria-current","step");else item.removeAttribute("aria-current");
    }
  }
  function renderDevices(){
    const list=$("device-list");list.replaceChildren();$("device-empty").hidden=state.devices.length>0;
    for(const device of state.devices){
      const status=deviceStatus(device);const ready=authorized(device);const button=create("button",`device-card${state.selected===device.serial?" selected":""}`);button.type="button";button.disabled=!ready;button.setAttribute("aria-pressed",String(state.selected===device.serial));
      const phone=create("span","phone-mark","▯");phone.setAttribute("aria-hidden","true");button.append(phone);
      const meta=create("span","device-meta");meta.append(create("span","device-name",str(device.model||device.name||"Android device").replace(/_/g," ")),create("span","device-serial",`USB · ${str(device.serial)}`));
      let label="Ready",kind="";
      if(!ready){kind="warning";if(status==="unauthorized"){label="Allow on phone";meta.append(create("span","device-help","Unlock the phone and approve “Allow USB debugging?”"));}else if(status==="offline"){label="Offline";meta.append(create("span","device-help","Reconnect the cable, unlock the phone and click Refresh."));}else{label=humanize(status)||"Unavailable";meta.append(create("span","device-help","A normal, authorized Android connection is required. See Connection guide."));}}
      button.append(meta,create("span",`status-badge ${kind}`,label));button.addEventListener("click",()=>selectDevice(device.serial));list.append(button);
    }
    $("device-inspection").hidden=!state.selected;updateControls();
  }
  async function refreshDevices(){
    if(state.busy.has("devices"))return;
    state.busy.add("devices");$("refresh-devices").disabled=true;
    try{
      const result=await api("/api/devices");state.devices=Array.isArray(result.devices)?result.devices:[];
      if(state.selected && !state.devices.some(d=>d.serial===state.selected && authorized(d))){state.selected=null;state.inspection=null;state.inspecting=false;}
      renderDevices();announce(state.devices.length?`${state.devices.length} connected device${state.devices.length===1?"":"s"} found.`:"No connected phones found.");
      const available=state.devices.filter(authorized);if(state.selected)await selectDevice(state.selected);else if(available.length===1)await selectDevice(available[0].serial);
    }finally{state.busy.delete("devices");updateControls();}
  }
  async function selectDevice(serial){
    if(!state.devices.some(d=>d.serial===serial && authorized(d)))return;
    state.selected=serial;state.inspection=null;state.inspecting=true;renderDevices();
    $("device-inspection").hidden=false;$("selected-model").textContent="Checking your phone";$("selected-os").textContent="";$("inspection-state").textContent="Looking for accessible storage and the optional helper…";$("storage-roots").replaceChildren();
    try{
      const result=await api("/api/inspect",{serial});if(state.selected!==serial)return;
      state.inspection=result.device||result;const info=state.inspection;
      $("selected-model").textContent=str(info.model||serial).replace(/_/g," ");$("selected-os").textContent=info.android?`Android ${info.android}`:"Android version unknown";
      const roots=Array.isArray(info.roots)?info.roots:[];$("inspection-state").textContent=roots.some(root=>root.accessible)?"Storage checked. Only folders the phone permits can be copied.":"No accessible storage confirmed. Check the phone’s unlock state and review the guide.";
      for(const root of roots){const row=create("div",`storage-root${root.accessible?"":" unavailable"}`);row.append(create("span","root-mark",root.accessible?"✓":"!"),create("span","",`${str(root.label||root.path)}${root.label?` · ${str(root.path)}`:""}${root.accessible?"":" · unavailable"}`));$("storage-roots").append(row);}
      const limitations=Array.isArray(info.limitations)?info.limitations:[];
      if(limitations.length){
        const details=create("details","inspection-limits");
        details.append(create("summary","","Access limits for this phone"));
        const list=create("ul","");
        for(const limitation of limitations)list.append(create("li","",str(limitation)));
        details.append(list);$("storage-roots").append(details);
      }
      announce("Phone selected and storage checked.");
    }catch(error){if(state.selected===serial){$("selected-model").textContent="Phone selected";$("inspection-state").textContent="Storage inspection failed. Refresh and try again before copying.";notice(error.message);}}
    finally{if(state.selected===serial){state.inspecting=false;updateControls();}}
  }
  function jobClass(status){return ["failed","error"].includes(status)?"error":["partial","cancelled","interrupted"].includes(status)?"warning":["running","queued","verifying","finalizing"].includes(status)?"neutral":"";}
  function renderVerification(info){
    const target=$("verify-result");target.hidden=false;
    const issues=Array.isArray(info.issues)?info.issues:[];
    const problems=issues.length+Number(info.missing||0)+Number(info.mismatched||info.changed||0)+(Array.isArray(info.errors)?info.errors.length:Number(info.errors||0));
    const ok=info.ok===true || info.valid===true || info.status==="verified" || info.status==="passed";
    target.className=`verification-result${ok?"":" warning"}`;
    target.textContent=str(info.message,ok?"Verification passed for the saved files listed in the archive inventory. This does not mean every category on the phone was accessible.":problems?`Verification found ${problems} issue${problems===1?"":"s"}. Open the coverage report for details.`:"Verification completed. Open the coverage report for the result and any missing or changed files.");
    if(Number.isFinite(Number(info.verified)) && Number.isFinite(Number(info.total)))target.textContent=`${number(info.verified)} of ${number(info.total)} saved files verified. ${problems?`${problems} issue${problems===1?"":"s"} need review. `:""}${str(info.meaning,"This verifies the archive files, not completeness of the phone’s data.")}`;
    if(info.checkedAt)target.append(create("div","",`Last checked: ${date(info.checkedAt)}`));
    for(const issue of issues.slice(0,8))target.append(create("div","",`${str(issue.path)}: ${str(issue.error)}`));
  }
  function renderJob(job){
    state.activeJob=job;$("active-job-panel").hidden=false;const running=isActive(job);const status=str(job.status).toLowerCase();
    $("job-heading").textContent=running?(status==="verifying" || job.analysisOnly || job.analysisStatus==="running"?"Checking your archive":status==="finalizing"?"Finalizing your archive":"Copying to your PC"):status==="completed"?"Transfer finished":status==="partial"?"Saved, with items to review":status==="cancelled"?"Transfer stopped":status==="failed"?"Transfer needs attention":"Your saved transfer";
    $("job-message").textContent=str(job.message,running?"Keep the phone connected.":"Review the coverage report to see what was saved and what was unavailable.");$("job-status").textContent=humanize(status);$("job-status").className=`status-badge ${jobClass(status)}`;
    $("job-phase").textContent=humanize(job.phase || (running?"Preparing":"Finished"));
    const total=Number(job.filesTotal)||0,copied=Number(job.filesCopied)||0;const percent=total>0?Math.min(100,Math.round(copied/total*100)):status==="completed"?100:0;
    // filesTotal grows during discovery; it is not a fixed work estimate.
    $("job-progress-fill").style.width=running?"25%":`${percent}%`;
    $("job-progress").classList.toggle("indeterminate",running);
    if(running)$("job-progress").removeAttribute("aria-valuenow");else $("job-progress").setAttribute("aria-valuenow",String(percent));
    const phase=str(job.phase).toLowerCase();
    $("job-percent").textContent=running?(status==="finalizing" || phase==="finalizing"?"Finishing archive…":phase.includes("cancel")?"Stopping safely…":status==="verifying" || phase.includes("verif")?"Checking saved files…":job.analysisOnly || job.analysisStatus==="running"?"Checking archive data…":"Finding and copying files…"):(total>0?`${number(copied)} copied / ${number(total)} listed`:status==="completed"?"Copy phase finished":"");
    $("job-file-count").textContent=!running && total>0?`${number(copied)} / ${number(total)}`:number(copied);$("job-byte-count").textContent=bytes(job.bytesCopied);$("job-destination").textContent=str(job.destination);
    $("cancel-job").hidden=!running;$("cancel-job").disabled=state.busy.has("job-action");$("resume-job").hidden=running || !["partial","failed","cancelled","interrupted"].includes(status);$("verify-job").hidden=running;$("open-job-report").disabled=!job.reportPath || state.busy.has("open-report");
    $("open-job-catalog").disabled=running || !job.destination || state.busy.has("open-catalog");
    $("open-job-reader").disabled=!state.config?.readerAvailable || running || !job.destination;
    $("open-job-folder").disabled=!job.destination || state.busy.has("open-folder");
    $("open-job-context").hidden=!job.deviceContext?.reportPath;$("open-job-context").disabled=running || state.busy.has("open-context");
    const coverage=$("job-coverage");coverage.replaceChildren();for(const item of Array.isArray(job.coverage)?job.coverage:[]){const row=create("div","coverage-row");const categoryStatus=str(item.status).toLowerCase();const warn=/unavailable|fail|partial|missing|skip|denied|not|unsupported|blocked|unverified|review/.test(categoryStatus);row.append(create("strong","",humanize(item.category)),create("span",`status-badge ${warn?"warning":""}`,humanize(item.status)),create("p","",str(item.detail)));coverage.append(row);}
    const errors=Array.isArray(job.errors)?job.errors:[];$("job-errors").hidden=errors.length===0;$("job-error-summary").textContent=`${errors.length} item${errors.length===1?"":"s"} to review`;$("job-error-list").replaceChildren();for(const error of errors.slice(0,200))$("job-error-list").append(create("li","",typeof error==="object"?str(error.message||error.error||JSON.stringify(error)):str(error)));if(errors.length>200)$("job-error-list").append(create("li","","More items are listed in the coverage report."));
    if(!running && job.verification)renderVerification(job.verification);else $("verify-result").hidden=true;
    $("archive-checks").hidden=!state.config?.archiveChecksAvailable;
    $("analyze-job").disabled=running || !job.destination || state.busy.has("job-action");
    const analysis=$("analysis-results");analysis.replaceChildren();
    const checks=[["phoneTrash","trash","Retained phone trash",r=>`${number(r.counts?.filesCopied)} of ${number(r.counts?.rows)} items saved`],["deletedItems","deleted","Saved-file deleted-item check",r=>`${number(r.counts?.fileCopiesSaved)} of ${number(r.counts?.fileCandidates)} trash-path files saved; ${number(r.counts?.messageCandidates)} flagged message rows`],["locationData","locations","Location check",r=>`${number(r.counts?.features)} location features; ${number(r.counts?.gpsMedia)} media files with GPS`]];
    if(running && checks.some(([key])=>job[key]))analysis.append(create("p","","Previous check results are shown below; current work is still running."));
    for(const [key,kind,label,summary] of checks){const result=job[key];const button=$("open-job-"+kind);button.hidden=!result?.reportPath;button.disabled=running;if(result)analysis.append(create("p","",`${label}: ${humanize(result.status)} — ${summary(result)}${result.error?". "+str(result.error):""}`));}
    if(!checks.some(([key])=>job[key]))analysis.append(create("p","",running?"These checks run after the transfer. Keep the phone connected for its retained-trash check.":"Not checked yet. Connect the original phone to include its retained trash; saved-file checks can also run offline."));
    const i=state.jobs.findIndex(item=>item.id===job.id);if(i<0)state.jobs.unshift(job);else state.jobs[i]=job;$("history-count").textContent=String(state.jobs.length);updateControls();
  }
  function startPolling(){if(state.poll)clearTimeout(state.poll);if(!isActive(state.activeJob))return;state.poll=setTimeout(pollJob,1500);}
  async function pollJob(){
    state.poll=null;const id=state.activeJob?.id;if(!id)return;
    try{const result=await api(`/api/jobs/${encodeURIComponent(id)}`);if(state.activeJob?.id!==id)return;const previous=state.activeJob.status;renderJob(result.job||result);state.pollFailures=0;if(!isActive(state.activeJob)){announce("Transfer stopped. Review your archive and coverage report.");if(previous!==state.activeJob.status)await refreshHistory();}}
    catch(error){state.pollFailures++;if(state.pollFailures===2)notice(error.message);}
    finally{if(isActive(state.activeJob))state.poll=setTimeout(pollJob,state.pollFailures>0?5000:1500);}
  }
  async function loadJob(id,{scroll=true}={}){
    const result=await api(`/api/jobs/${encodeURIComponent(id)}`);$("verify-result").hidden=true;renderJob(result.job||result);switchView("transfer");startPolling();if(scroll)$("active-job-panel").scrollIntoView({behavior:"smooth",block:"start"});
  }
  function openReader(job){
    if(!state.config?.readerAvailable || !job?.id || !job.destination || isActive(job))return;
    window.location.assign("/reader?job="+encodeURIComponent(job.id));
  }
  function renderHistory(){
    const list=$("history-list");list.replaceChildren();$("history-count").textContent=String(state.jobs.length);$("history-empty").hidden=state.jobs.length>0;
    for(const job of state.jobs){const card=create("article","panel history-card");const icon=create("div","history-card-icon","↙");icon.setAttribute("aria-hidden","true");card.append(icon);const body=create("div","history-card-body"),title=create("div","history-card-title");title.append(create("h2","",str(job.model||job.deviceModel||job.serial||"Android archive").replace(/_/g," ")),create("span",`status-badge ${jobClass(str(job.status))}`,humanize(job.status)));body.append(title,create("p","",`${date(job.startedAt)} · ${number(job.filesCopied)} files · ${bytes(job.bytesCopied)}`),create("p","history-card-path",str(job.destination)));const actions=create("div","history-card-actions");const review=create("button","text-button",isActive(job)?"View progress →":"Review transfer →");review.addEventListener("click",()=>perform("load-job",review,"Opening…",()=>loadJob(job.id)));actions.append(review);const reader=create("button","text-button","Read messages & media");reader.disabled=!state.config?.readerAvailable||isActive(job)||!job.destination;reader.addEventListener("click",()=>openReader(job));actions.append(reader);const folder=create("button","text-button","Open folder");folder.disabled=!job.destination;folder.addEventListener("click",()=>perform("open-history-folder",folder,"Opening…",()=>api(`/api/jobs/${encodeURIComponent(job.id)}/open`,{kind:"folder"})));actions.append(folder);if(job.reportPath){const report=create("button","text-button","Open report ↗");report.addEventListener("click",()=>perform("open-history-report",report,"Opening…",()=>api(`/api/jobs/${encodeURIComponent(job.id)}/open`,{kind:"report"})));actions.append(report);}body.append(actions);card.append(body);list.append(card);}
  }
  async function refreshHistory(){const result=await api("/api/jobs");state.jobs=(Array.isArray(result.jobs)?result.jobs:[]).slice().sort((a,b)=>(new Date(b.startedAt).getTime()||0)-(new Date(a.startedAt).getTime()||0));renderHistory();updateControls();}
  async function startTransfer(){
    const selected=state.devices.find(d=>d.serial===state.selected);if(!authorized(selected))throw new Error("Select an authorized phone before starting.");
    const extraRoots=$("extra-roots").value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);if(extraRoots.some(path=>!path.startsWith("/") || /[\0\r\n]/.test(path)))throw new Error("Additional folders must be absolute Android paths, one per line, beginning with /.");
    const options={shared:$("option-shared").checked,apks:$("option-apks").checked,helper:$("option-helper").checked,legacy:$("option-legacy").checked,privateApps:$("option-private").checked,existingRoot:$("option-root").checked,deviceContext:$("option-context").checked,roots:extraRoots.length?[...new Set([...(state.inspection?.roots||[]).filter(root=>root.accessible).map(root=>root.path),...extraRoots])]:[]};
    const result=await api("/api/jobs",{serial:state.selected,destination:$("destination").value.trim(),options});const job=result.job||result;$("verify-result").hidden=true;renderJob(job);startPolling();$("active-job-panel").scrollIntoView({behavior:"smooth",block:"start"});announce("Transfer started. Follow its progress below.");
  }
  async function jobAction(action){
    const id=state.activeJob?.id;if(!id)return;const result=await api(`/api/jobs/${encodeURIComponent(id)}/${action}`,{});
    if(action==="verify"){
      if(result.job)renderJob(result.job);else{const latest=await api(`/api/jobs/${encodeURIComponent(id)}`);renderJob(latest.job||latest);}
      renderVerification(result.verification||result.result||result);
    }else if(result.job)renderJob(result.job);else if(result.id)renderJob(result);else{const latest=await api(`/api/jobs/${encodeURIComponent(id)}`);renderJob(latest.job||latest);}
    startPolling();await refreshHistory();announce(action==="cancel"?"Stop requested. Files already copied stay in the archive.":action==="resume"?"Transfer resumed.":action==="analyze"?"Deleted-item and location checks started.":"Verification result is available.");
  }
  function bind(){
    for(const button of document.querySelectorAll("[data-view]"))button.addEventListener("click",()=>switchView(button.dataset.view,{focus:true}));
    for(const button of document.querySelectorAll("[data-open-guide]"))button.addEventListener("click",()=>{switchView("guide",{focus:true});window.scrollTo({top:0,behavior:"smooth"});});
    for(const button of document.querySelectorAll("[data-return-transfer]"))button.addEventListener("click",()=>{switchView("transfer",{focus:true});window.scrollTo({top:0,behavior:"smooth"});});
    for(const button of document.querySelectorAll("[data-open-coverage]"))button.addEventListener("click",()=>{switchView("guide");$("coverage-guide").scrollIntoView({behavior:"smooth",block:"start"});$("coverage-guide").focus({preventScroll:true});});
    document.querySelector(".brand").addEventListener("click",event=>{event.preventDefault();switchView("transfer",{focus:true});});
    $("dismiss-notice").addEventListener("click",clearNotice);
    $("refresh-devices").addEventListener("click",()=>{clearNotice();refreshDevices().catch(error=>notice(error.message));});
    $("refresh-history").addEventListener("click",()=>perform("history",$("refresh-history"),"Refreshing…",refreshHistory));
    $("destination").addEventListener("input",updateControls);$("extra-roots").addEventListener("input",updateControls);
    for(const id of ["option-shared","option-apks","option-helper","option-legacy","option-private","option-root","option-context"])$(id).addEventListener("change",updateControls);
    $("browse-folder").addEventListener("click",()=>perform("browse",$("browse-folder"),"Choosing…",async()=>{const result=await api("/api/pick-folder",{});if(result.path){$("destination").value=result.path;updateControls();announce("Destination folder selected.");}}));
    $("install-helper").addEventListener("click",()=>perform("helper",$("install-helper"),"Installing…",async()=>{const result=await api("/api/helper/install",{serial:state.selected});notice(str(result.message,"Phone helper installed. Open it on the phone, review permissions and finish an export before copying."),"success");await selectDevice(state.selected);}));
    $("open-helper").addEventListener("click",()=>perform("helper",$("open-helper"),"Opening…",async()=>{await api("/api/helper/open",{serial:state.selected});notice("Look at your phone: review the permissions and create an export in Android Bay Helper. Return here when it finishes.","success");}));
    $("mirror-phone").addEventListener("click",()=>perform("mirror",$("mirror-phone"),"Opening…",async()=>{const result=await api("/api/mirror",{serial:state.selected});notice(str(result.message,"Phone mirror opened in a separate window. Use each app’s export option, then copy the exported files."),"success");}));
    $("start-transfer").addEventListener("click",()=>perform("start",$("start-transfer"),"Starting…",startTransfer));
    $("open-job-reader").addEventListener("click",()=>openReader(state.activeJob));
    for(const [id,action,label] of [["cancel-job","cancel","Stopping…"],["resume-job","resume","Resuming…"],["verify-job","verify","Verifying…"],["analyze-job","analyze","Starting checks…"]])$(id).addEventListener("click",()=>perform("job-action",$(id),label,()=>jobAction(action)));
    for(const kind of ["folder","report","catalog","deleted","locations","trash","context"])$("open-job-"+kind).addEventListener("click",()=>perform("open-"+kind,$("open-job-"+kind),"Opening…",()=>api(`/api/jobs/${encodeURIComponent(state.activeJob.id)}/open`,{kind})));
  }
  async function init(){
    bind();updateControls();
    try{
      state.config=await api("/api/config");state.connected=true;$("demo-banner").hidden=!state.config.demo;$("version-label").textContent=`v${str(state.config.version,"1.0").replace(/^v/,"")}`;$("destination").value=str(state.config.defaultDestination);$("adb-warning").hidden=Boolean(state.config.adbAvailable);updateControls();
      const results=await Promise.allSettled([refreshDevices(),refreshHistory()]);for(const result of results)if(result.status==="rejected")notice(result.reason.message);
      const running=state.jobs.find(isActive);if(running){await loadJob(running.id,{scroll:false});}
      announce("Android Bay is ready.");
    }catch(error){state.connected=false;document.body.classList.add("api-failed");notice(error.message);}
    finally{$("loading-state").hidden=true;updateControls();}
  }
  init();
})();
