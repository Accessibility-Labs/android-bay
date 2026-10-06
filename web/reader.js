(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {
    jobId: new URLSearchParams(location.search).get("job") || "", config: null, job: null,
    index: null, tab: "messages", thread: null, threads: [], messages: [], media: [],
    threadOffset: 0, threadTotal: 0, messageStart: 0, messageNext: 0, messageTotal: 0,
    mediaStart: 0, mediaNext: 0, mediaTotal: 0, poll: null, building: false, needsRebuild: false,
    requests: new Map(), timers: new Map(), dialogTrigger: null
  };
  const PAGE_THREADS = 60, PAGE_MESSAGES = 100, PAGE_MEDIA = 48;
  const MAX_MESSAGES = 500, MAX_MEDIA = 192;
  const nf = new Intl.NumberFormat();
  const dateFormat = new Intl.DateTimeFormat(undefined, {year:"numeric", month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
  const shortDateFormat = new Intl.DateTimeFormat(undefined, {month:"short", day:"numeric", year:"numeric"});
  const text = value => value == null ? "" : String(value);
  const number = value => Number.isFinite(Number(value)) ? nf.format(Number(value)) : "—";
  const count = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const make = (tag, className, value) => {const el=document.createElement(tag);if(className)el.className=className;if(value!=null)el.textContent=text(value);return el;};
  const apiBase = () => `/api/jobs/${encodeURIComponent(state.jobId)}`;
  const readerBase = () => `${apiBase()}/reader`;
  const mediaURL = (id, download=false) => `${readerBase()}/media/${encodeURIComponent(text(id))}${download?"?download=1":""}`;
  const validMediaId = id => /^[a-f0-9]{24}$/i.test(text(id));
  const formatBytes = value => {const n=Number(value);if(!Number.isFinite(n)||n<0)return "Size unknown";if(n<1024)return `${number(n)} B`;const i=Math.min(4,Math.floor(Math.log(n)/Math.log(1024)));return `${(n/1024**i).toLocaleString(undefined,{maximumFractionDigits:1})} ${["B","KB","MB","GB","TB"][i]}`;};
  function dateOf(item, thread=false) {
    const ms=thread?item.lastDateMs:item.dateMs;
    const value=ms!=null?ms:thread?(item.lastDateIso??item.lastTimestamp):(item.dateIso??item.timestamp);
    if(value==null||value==="")return null;
    const date=new Date(value);return Number.isFinite(date.getTime())?date:null;
  }
  function announce(message){$("live-status").textContent=message;}
  function notice(message){$("notice-text").textContent=text(message);$("notice").hidden=false;}
  function hideNotice(){$("notice").hidden=true;}
  function errorMessage(error){return error instanceof Error?error.message:"Something went wrong. Try again.";}
  function debounce(key, fn){clearTimeout(state.timers.get(key));state.timers.set(key,setTimeout(fn,300));}
  async function api(path, body, signal) {
    const options={method:body===undefined?"GET":"POST",credentials:"same-origin",cache:"no-store",signal,headers:{"Accept":"application/json"}};
    if(body!==undefined){options.headers["Content-Type"]="application/json";options.headers["X-CSRF-Token"]=state.config?.csrfToken||"";options.body=JSON.stringify(body);}
    const response=await fetch(path,options);
    let result;
    try{result=await response.json();}catch{throw new Error(`The local service returned an unreadable response (${response.status}). Reopen Android Bay and try again.`);}
    if(!response.ok)throw new Error(text(result.error)||`The local service could not complete this request (${response.status}).`);
    return result;
  }
  function beginRequest(key){state.requests.get(key)?.abort();const controller=new AbortController();state.requests.set(key,controller);return controller;}
  function isCurrent(key, controller){return state.requests.get(key)===controller&&!controller.signal.aborted;}
  function itemsOf(result){return Array.isArray(result.items)?result.items:[];}
  function totalOf(result){return count(result.total);}
  function queryURL(route, params){return `${readerBase()}/${route}?${new URLSearchParams(params)}`;}
  function setButtonBusy(button, busy){button.disabled=busy;button.setAttribute("aria-busy",String(busy));}
  function activeJob(){return ["running","finalizing","verifying"].includes(state.job?.status)||state.job?.analysisStatus==="running";}

  function showIndex(stateName, message) {
    const busy=["opening","building","indexing","running"].includes(stateName);
    state.building=busy;
    $("index-panel").hidden=false;$("reader-content").hidden=true;
    $("index-spinner").hidden=!busy;
    $("index-heading").textContent=stateName==="opening"?"Opening your archive":busy?"Preparing your archive reader":stateName==="missing"?"Make your saved data easy to browse":stateName==="stale"?"Your saved archive has changed":"The archive index needs attention";
    $("index-message").textContent=message||(busy?"Reading saved records and matching media files. Large archives can take a few minutes.":"Build the local index to browse this archive.");
    $("build-index").hidden=busy;$("build-index").disabled=!state.config||activeJob();
    $("build-index").textContent=stateName==="missing"?"Build archive index":"Try building again";
    if(activeJob()&&!busy)$("index-message").textContent="This archive is still being copied or checked. Wait for that operation to finish, then reopen the reader.";
  }
  function renderCoverage() {
    const data=state.index||{}, counts=data.counts||{}, issues=Array.isArray(data.issues)?data.issues:[];
    const coverage=Array.isArray(state.job?.coverage)?state.job.coverage:[];
    const errors=Array.isArray(state.job?.errors)?state.job.errors:[];
    const partial=data.state==="partial"||state.job?.status==="partial"||issues.length>0;
    $("archive-status").textContent=partial?"Items to review":"Saved copy";
    $("archive-status").classList.toggle("warning",partial);
    $("coverage-summary").textContent=partial?`Coverage & limitations · ${number(counts.issues??issues.length)} index issues${errors.length?` · ${number(errors.length)} transfer issues`:""}`:"Coverage & limitations · what this reader includes";
    const box=$("coverage-content");box.replaceChildren();
    box.append(make("p","","This reader shows indexed SMS/MMS exports and recognized media from the saved PC archive. It does not establish a complete phone backup. Private app chats, RCS, cloud-only data and erased records may be missing."));
    if(counts.sms!=null||counts.mms!=null)box.append(make("p","",`${number(counts.sms)} SMS records · ${number(counts.mms)} MMS records · ${number(counts.missingAttachments)} unavailable attachment references. Record counts are not counts of unique conversations or files.`));
    if(activeJob())box.append(make("p","","A transfer or archive check is active. The reader may show an earlier index until that operation finishes and you rebuild it."));
    const limitations=Array.isArray(data.limitations)?data.limitations:[];
    for(const limit of limitations)box.append(make("p","",typeof limit==="string"?limit:JSON.stringify(limit)));
    const needsReview=coverage.filter(item=>/partial|failed|blocked|unverified|review|skipped|unavailable|unsupported|missing/i.test(text(item.status)));
    if(needsReview.length){box.append(make("strong","","Transfer categories to review"));const ul=make("ul");for(const row of needsReview)ul.append(make("li","",`${text(row.category).replaceAll("_"," ")}: ${text(row.status)}. ${text(row.detail)}`));box.append(ul);}
    if(issues.length){box.append(make("strong","","Index diagnostics"));const ul=make("ul");for(const issue of issues.slice(0,30))ul.append(make("li","",typeof issue==="string"?issue:`${text(issue.detail||issue.code||"Index issue")}${issue.count!=null?` (${number(issue.count)})`:""}`));box.append(ul);}
    if(errors.length){box.append(make("p","",`${number(errors.length)} acquisition issue${errors.length===1?"":"s"} appear in the coverage report. Use Coverage report above for the original transfer results.`));}
  }
  function acceptIndex(data) {
    state.index=data;state.building=false;state.needsRebuild=false;
    state.requests.get("messages")?.abort();state.thread=null;state.messages=[];
    $("conversation-view").hidden=true;$("message-empty").hidden=false;$("message-list").replaceChildren();
    $("index-panel").hidden=true;$("reader-content").hidden=false;
    const counts=data.counts||{};
    for(const key of ["messages","photos","videos","audio"])$("count-"+key).textContent=counts[key]==null?"":number(counts[key]);
    $("archive-counts").textContent=`${number(counts.threads)} conversations · ${number(counts.media)} media files`;
    const model=state.job?.model||state.job?.deviceModel||state.job?.device?.model;
    $("archive-label").textContent=model?`${text(model)} · Saved on this PC`:"Saved on this PC · Select a category below";
    $("rebuild-index").disabled=activeJob();
    renderCoverage();
    if(state.tab==="messages")loadThreads(false);else loadMedia(false);
    announce("Archive reader ready. Select a conversation or media category.");
  }
  async function pollIndex(autoBuild=false) {
    clearTimeout(state.poll);
    try{
      const result=await api(`${readerBase()}/status`);
      const data=result.reader||result;
      const status=text(data.state||data.status).toLowerCase();
      if(["ready","partial","completed"].includes(status)){acceptIndex({...data,state:status});return;}
      if(["building","indexing","running"].includes(status)){
        showIndex("building",text(data.message)||text(data.progress?.message));
        state.poll=setTimeout(()=>pollIndex(false),1500);return;
      }
      state.needsRebuild=status==="stale";
      showIndex(status||"missing",data.message||data.error||(status==="stale"?"Rebuild the local index to include the current saved files.":""));
      if(autoBuild&&(!status||status==="missing"||status==="stale")&&!activeJob())await buildIndex(state.needsRebuild);
    }catch(error){showIndex("failed",errorMessage(error));}
  }
  async function buildIndex(rebuild=false) {
    if(state.building)return;
    hideNotice();showIndex("building","Starting the local index. Your original files will not be changed.");
    try{await api(`${readerBase()}/build`,rebuild?{rebuild:true}:{});await pollIndex(false);}
    catch(error){showIndex("failed",errorMessage(error));}
  }

  async function openArchive(kind, button) {
    if(button)setButtonBusy(button,true);
    try{await api(`${apiBase()}/open`,{kind});announce(kind==="catalog"?"Opening the original saved-file catalog.":"Opening the saved archive item.");}
    catch(error){notice(errorMessage(error));}
    finally{if(button)setButtonBusy(button,false);}
  }
  function changeTab(tab, focus=false) {
    state.tab=tab;
    for(const button of document.querySelectorAll("[data-tab]")){const selected=button.dataset.tab===tab;button.setAttribute("aria-selected",String(selected));button.tabIndex=selected?0:-1;if(selected&&focus)button.focus();}
    const messages=tab==="messages";$("messages-panel").hidden=!messages;$("media-panel").hidden=messages;
    if(!messages){$("media-panel").setAttribute("aria-labelledby",`tab-${tab}`);$("media-heading").textContent=tab==="audio"?"Audio":tab==="videos"?"Videos":"Photos";loadMedia(false);}
  }
  function conversationTitle(thread){return text(thread.title)||"Unknown conversation";}
  function participantLabels(participants){return (Array.isArray(participants)?participants:[]).map(p=>[text(p.name),text(p.address)].filter(Boolean).join(" · ")).filter(Boolean).join(", ");}
  function renderThreads() {
    const list=$("thread-list");list.replaceChildren();
    if(!state.threads.length){list.append(make("p","empty-thread",$("thread-search").value.trim()?"No matching conversations. Try a name, number or another phrase.":"No SMS or MMS conversations were indexed. Check the coverage report and whether the phone helper finished exporting."));}
    for(const thread of state.threads){
      const button=make("button","thread-item");button.type="button";button.classList.toggle("selected",state.thread?.id===thread.id);button.setAttribute("aria-pressed",String(state.thread?.id===thread.id));
      const title=conversationTitle(thread), avatar=make("span","thread-avatar",title.trim().slice(0,2).toUpperCase());avatar.setAttribute("aria-hidden","true");
      const content=make("span","thread-content"), top=make("span","thread-topline");top.append(make("span","thread-title",title));const date=dateOf(thread,true);if(date)top.append(make("span","thread-date",shortDateFormat.format(date)));
      content.append(top,make("span","thread-snippet",participantLabels(thread.participants)||"Saved SMS / MMS"),make("span","thread-total",`${number(thread.messageCount??thread.count)} saved records`));
      button.append(avatar,content);button.addEventListener("click",()=>selectThread(thread));list.append(button);
    }
    $("thread-status").textContent=`${number(state.threadTotal)} conversation${state.threadTotal===1?"":"s"}${$("thread-search").value.trim()?" matching your search":""}`;
    $("more-threads").hidden=state.threadOffset>=state.threadTotal;
  }
  async function loadThreads(append=false) {
    const controller=beginRequest("threads"), offset=append?state.threadOffset:0;
    setButtonBusy($("more-threads"),true);$("thread-status").textContent="Searching saved conversations…";
    if(!append){state.threads=[];$("thread-list").replaceChildren(make("p","loading-text","Loading conversations…"));$("more-threads").hidden=true;}
    try{
      const result=await api(queryURL("threads",{q:$("thread-search").value.trim(),offset:String(offset),limit:String(PAGE_THREADS)}),undefined,controller.signal);
      if(!isCurrent("threads",controller))return;
      const rows=itemsOf(result);state.threads=append?[...state.threads,...rows]:rows;state.threadTotal=totalOf(result);state.threadOffset=offset+rows.length;renderThreads();
    }catch(error){if(error.name!=="AbortError"&&isCurrent("threads",controller)){$("thread-status").textContent="Conversations could not be loaded.";$("thread-list").replaceChildren(make("p","load-error",errorMessage(error)));}}
    finally{if(isCurrent("threads",controller))setButtonBusy($("more-threads"),false);}
  }
  function selectThread(thread) {
    state.thread=thread;$("message-search").value="";
    $("message-empty").hidden=true;$("conversation-view").hidden=false;
    $("conversation-title").textContent=conversationTitle(thread);$("conversation-participants").textContent=participantLabels(thread.participants);
    renderThreads();loadMessages(false,0);$("conversation-title").focus({preventScroll:true});
    if(matchMedia("(max-width: 720px)").matches)$("conversation-view").scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});
  }
  function directionLabel(message) {return {incoming:"Received",outgoing:"Outgoing",draft:"Draft",failed:"Failed",queued:"Queued",unknown:"Direction unknown"}[message.direction]||"Direction unknown";}
  function attachmentKind(item){const kind=text(item.kind);if(["photo","video","audio"].includes(kind))return kind;const mime=text(item.mime||item.contentType).toLowerCase();return mime.startsWith("image/")?"photo":mime.startsWith("video/")?"video":mime.startsWith("audio/")?"audio":"other";}
  function attachmentNode(item) {
    const wrap=make("div","attachment"), name=text(item.name)||"MMS attachment";
    if(item.available===false||!validMediaId(item.id)){wrap.append(make("div","attachment-unavailable",`${name} · Original attachment unavailable in this archive`));return wrap;}
    const kind=attachmentKind(item), button=make("button","attachment-preview");button.type="button";button.setAttribute("aria-label",`Open ${name}`);
    if(kind==="photo"){
      const img=make("img");img.loading="lazy";img.decoding="async";img.alt=name;img.src=mediaURL(item.id);img.addEventListener("error",()=>{button.replaceChildren(make("span","attachment-icon","▧ Preview unavailable · open file options"));},{once:true});button.append(img);
    }else button.append(make("span","attachment-icon",`${kind==="video"?"▶":kind==="audio"?"♫":"↗"} ${name}`));
    button.addEventListener("click",()=>openMedia({...item,kind},button));wrap.append(button);
    const link=make("a","",`Save original · ${name}`);link.href=mediaURL(item.id,true);link.download="";wrap.append(link);return wrap;
  }
  function messageNode(message) {
    const article=make("article",`message ${message.direction==="outgoing"?"outgoing":""}`), meta=make("div","message-meta"), date=dateOf(message);
    meta.append(make("span","",directionLabel(message)),make("span","",date?dateFormat.format(date):"Time unavailable"),make("span","",text(message.kind).toUpperCase()));
    const bubble=make("div","message-bubble"), body=text(message.body??message.text), subject=text(message.subject);
    if(subject)bubble.append(make("p","message-subject",subject));
    if(body)bubble.append(make("p","message-body",body));
    const attachments=Array.isArray(message.attachments)?message.attachments:[];
    if(!body&&!subject&&!attachments.length)bubble.append(make("p","message-missing","No readable body or attachment was available in this saved record."));
    if(attachments.length){const block=make("div","message-attachments");for(const attachment of attachments)block.append(attachmentNode(attachment));bubble.append(block);}
    const details=make("details"), summary=make("summary","","Record details"), raw=make("pre","",JSON.stringify({recordId:message.androidId,kind:message.kind,mailbox:message.box,direction:message.direction,rawDate:message.dateRaw,normalizedDate:message.dateIso??message.timestamp,participants:message.participants,source:message.source},null,2));details.append(summary,raw);article.append(meta,bubble,details);return article;
  }
  function renderMessages() {
    const list=$("message-list");list.replaceChildren();
    if(!state.messages.length){const box=make("div","empty-state");box.append(make("h2","","No matching messages"),make("p","",$("message-search").value.trim()?"Try another word or clear the conversation search.":"No readable SMS or MMS records were available for this conversation."));list.append(box);}
    else{const fragment=document.createDocumentFragment();for(const row of state.messages)fragment.append(messageNode(row));list.append(fragment);}
    $("message-status").textContent=state.messages.length?`Showing ${number(state.messageStart+1)}–${number(state.messageStart+state.messages.length)} of ${number(state.messageTotal)} saved records · oldest first`:`${number(state.messageTotal)} matching records`;
    $("first-messages").hidden=state.messageStart===0;$("previous-messages").hidden=state.messageStart===0;$("more-messages").hidden=state.messageNext>=state.messageTotal;$("last-messages").hidden=state.messageNext>=state.messageTotal;
  }
  async function loadMessages(append=false, requestedOffset=0) {
    if(!state.thread)return;
    const controller=beginRequest("messages"), offset=append?state.messageNext:requestedOffset, threadId=state.thread.id;
    for(const id of ["first-messages","previous-messages","more-messages","last-messages"])setButtonBusy($(id),true);
    $("message-status").textContent="Loading saved messages…";
    if(!append){$("message-list").replaceChildren(make("p","loading-text","Reading saved records…"));for(const id of ["first-messages","previous-messages","more-messages","last-messages"])$(id).hidden=true;}
    try{
      const result=await api(queryURL("messages",{thread:text(threadId),q:$("message-search").value.trim(),offset:String(offset),limit:String(PAGE_MESSAGES)}),undefined,controller.signal);
      if(!isCurrent("messages",controller)||state.thread?.id!==threadId)return;
      const rows=itemsOf(result);state.messageTotal=totalOf(result);state.messageNext=offset+rows.length;
      if(append)state.messages.push(...rows);else{state.messages=rows;state.messageStart=offset;}
      if(state.messages.length>MAX_MESSAGES){const drop=state.messages.length-MAX_MESSAGES;state.messages.splice(0,drop);state.messageStart+=drop;}
      const oldScroll=$("message-list").scrollTop;renderMessages();$("message-list").scrollTop=append?oldScroll:0;
      announce(`${number(rows.length)} saved message${rows.length===1?"":"s"} loaded.`);
    }catch(error){if(error.name!=="AbortError"&&isCurrent("messages",controller)){$("message-status").textContent="Messages could not be loaded.";$("message-list").replaceChildren(make("p","load-error",errorMessage(error)));}}
    finally{if(isCurrent("messages",controller))for(const id of ["first-messages","previous-messages","more-messages","last-messages"])setButtonBusy($(id),false);}
  }

  function renderMedia() {
    const grid=$("media-grid");grid.replaceChildren();
    if(!state.media.length){const empty=make("div","empty-state");empty.append(make("div","empty-icon",state.tab==="videos"?"▶":state.tab==="audio"?"♫":"▧"),make("h2","",$("media-search").value.trim()?"No matching files":`No ${state.tab} were indexed`),make("p","",$("media-search").value.trim()?"Try a shorter filename or clear the search.":"Check All saved files and the coverage report. Unsupported types or protected phone files may not be available here."));grid.append(empty);}
    for(const item of state.media){
      const kind=attachmentKind(item), name=text(item.name)||"Unnamed saved file", button=make("button",`media-card ${kind}`);button.type="button";button.setAttribute("aria-label",`Open ${name}`);
      const thumbnail=make("span","media-thumbnail");
      if(kind==="photo"&&validMediaId(item.id)){
        const img=make("img");img.src=mediaURL(item.id);img.alt="";img.loading="lazy";img.decoding="async";img.addEventListener("error",()=>thumbnail.replaceChildren(make("span","media-glyph","▧")),{once:true});thumbnail.append(img);
      }else thumbnail.append(make("span","media-glyph",kind==="video"?"▶":kind==="audio"?"♫":"▧"));
      const body=make("span","media-card-body"), info=make("span","media-card-info");info.append(make("span","",formatBytes(item.size)),make("span","",kind==="video"?"Play video":kind==="audio"?"Play audio":"View photo"));body.append(make("span","media-card-name",name),info);button.append(thumbnail,body);button.addEventListener("click",()=>openMedia({...item,kind},button));grid.append(button);
    }
    $("media-status").textContent=state.media.length?`Showing ${number(state.mediaStart+1)}–${number(state.mediaStart+state.media.length)} of ${number(state.mediaTotal)} files`:`${number(state.mediaTotal)} files`;
    $("previous-media").hidden=state.mediaStart===0;$("more-media").hidden=state.mediaNext>=state.mediaTotal;
  }
  async function loadMedia(append=false, requestedOffset=0) {
    const controller=beginRequest("media"), offset=append?state.mediaNext:requestedOffset, tab=state.tab;
    if(tab==="messages")return;
    for(const id of ["previous-media","more-media"])setButtonBusy($(id),true);
    $("media-status").textContent="Finding saved files…";
    if(!append){$("media-grid").replaceChildren(make("p","loading-text","Loading files from this archive…"));$("previous-media").hidden=true;$("more-media").hidden=true;}
    try{
      const result=await api(queryURL("media",{kind:tab==="photos"?"photo":tab==="videos"?"video":"audio",q:$("media-search").value.trim(),offset:String(offset),limit:String(PAGE_MEDIA)}),undefined,controller.signal);
      if(!isCurrent("media",controller)||state.tab!==tab)return;
      const rows=itemsOf(result);state.mediaTotal=totalOf(result);state.mediaNext=offset+rows.length;
      if(append)state.media.push(...rows);else{state.media=rows;state.mediaStart=offset;}
      if(state.media.length>MAX_MEDIA){const drop=state.media.length-MAX_MEDIA;state.media.splice(0,drop);state.mediaStart+=drop;}
      renderMedia();announce(`${number(rows.length)} ${tab==="photos"?"photo":tab==="videos"?"video":"audio"} file${rows.length===1?"":"s"} loaded.`);
    }catch(error){if(error.name!=="AbortError"&&isCurrent("media",controller)){$("media-status").textContent="Files could not be loaded.";$("media-grid").replaceChildren(make("p","load-error",errorMessage(error)));}}
    finally{if(isCurrent("media",controller))for(const id of ["previous-media","more-media"])setButtonBusy($(id),false);}
  }
  function openMedia(item, trigger) {
    if(!validMediaId(item.id)){notice("This item has no available original file. Review its source record in the archive.");return;}
    closePlayer();state.dialogTrigger=trigger||document.activeElement;
    const kind=attachmentKind(item), preview=$("media-preview"), name=text(item.name)||"Saved attachment";
    $("media-dialog-title").textContent=name;$("media-dialog-kind").textContent=`SAVED ${kind==="photo"?"PHOTO":kind==="video"?"VIDEO":kind==="audio"?"AUDIO":"FILE"} · ${formatBytes(item.size)}`;
    const note=$("media-playback-note");note.classList.remove("error");note.textContent=kind==="photo"?"This preview reads the saved original. Use Save original file to keep another copy.":kind==="other"?"This file type has no built-in preview. Save the original and open it with a suitable app on your PC.":"Press play to start. If the browser cannot play this format, save the original and open it in your PC’s media player.";
    if(kind==="photo"){
      const img=make("img");img.alt=name;img.src=mediaURL(item.id);img.addEventListener("error",()=>{preview.replaceChildren(make("p","preview-fallback","This image could not be previewed. Save the original file to open it in a compatible app."));note.classList.add("error");},{once:true});preview.append(img);
    }else if(kind==="video"||kind==="audio"){
      const player=make(kind);player.controls=true;player.preload="none";player.src=mediaURL(item.id);if(kind==="video")player.playsInline=true;
      player.addEventListener("error",()=>{note.textContent="Playback is unavailable for this saved file. The browser may not support its codec, or the file may be incomplete. Save the original and try a compatible PC player.";note.classList.add("error");});preview.append(player);
    }else preview.append(make("p","preview-fallback","Preview unavailable for this file type."));
    $("download-media").href=mediaURL(item.id,true);$("download-media").download="";
    $("media-source").textContent=JSON.stringify({name:item.name,type:item.mime||item.contentType,size:item.size,source:item.source,sha256:item.sha256||undefined},null,2);
    if(!$("media-dialog").open)$("media-dialog").showModal();$("close-media").focus();
  }
  function closePlayer(){const preview=$("media-preview");for(const player of preview.querySelectorAll("video,audio")){player.pause();player.removeAttribute("src");player.load();}preview.replaceChildren();}
  function closeDialog(){if($("media-dialog").open)$("media-dialog").close();}
  function bind() {
    $("dismiss-notice").addEventListener("click",hideNotice);
    $("build-index").addEventListener("click",()=>buildIndex(state.needsRebuild));$("rebuild-index").addEventListener("click",()=>buildIndex(true));
    for(const [id,kind] of [["open-catalog","catalog"],["open-report","report"],["open-folder","folder"],["dialog-open-folder","folder"]])$(id).addEventListener("click",()=>openArchive(kind,$(id)));
    const tabs=[...document.querySelectorAll("[data-tab]")];
    for(const button of tabs){button.addEventListener("click",()=>changeTab(button.dataset.tab));button.addEventListener("keydown",event=>{let next=tabs.indexOf(button);if(event.key==="ArrowRight")next=(next+1)%tabs.length;else if(event.key==="ArrowLeft")next=(next-1+tabs.length)%tabs.length;else if(event.key==="Home")next=0;else if(event.key==="End")next=tabs.length-1;else return;event.preventDefault();changeTab(tabs[next].dataset.tab,true);});}
    $("thread-search").addEventListener("input",()=>debounce("threads",()=>loadThreads(false)));$("more-threads").addEventListener("click",()=>loadThreads(true));
    $("message-search").addEventListener("input",()=>debounce("messages",()=>loadMessages(false,0)));$("more-messages").addEventListener("click",()=>loadMessages(true));$("previous-messages").addEventListener("click",()=>loadMessages(false,Math.max(0,state.messageStart-PAGE_MESSAGES)));
    $("first-messages").addEventListener("click",()=>loadMessages(false,0));$("last-messages").addEventListener("click",()=>loadMessages(false,Math.max(0,state.messageTotal-PAGE_MESSAGES)));
    $("media-search").addEventListener("input",()=>debounce("media",()=>loadMedia(false,0)));$("more-media").addEventListener("click",()=>loadMedia(true));$("previous-media").addEventListener("click",()=>loadMedia(false,Math.max(0,state.mediaStart-PAGE_MEDIA)));
    $("close-media").addEventListener("click",closeDialog);$("media-dialog").addEventListener("close",()=>{closePlayer();state.dialogTrigger?.focus?.();});
    $("media-dialog").addEventListener("click",event=>{if(event.target!==$("media-dialog"))return;const rect=$("media-dialog").getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeDialog();});
    window.addEventListener("pagehide",()=>{clearTimeout(state.poll);for(const controller of state.requests.values())controller.abort();closePlayer();});
  }
  async function init() {
    bind();$("timezone-note").textContent=`Times: ${Intl.DateTimeFormat().resolvedOptions().timeZone||"PC local timezone"}. Raw dates in Record details.`;
    if(!/^[a-z0-9_-]{1,128}$/i.test(state.jobId)){showIndex("failed","No valid archive was selected. Return to Android Bay → Saved transfers → Read messages & media.");$("build-index").hidden=true;return;}
    try{
      state.config=await api("/api/config");$("demo-banner").hidden=!state.config.demo;
      const result=await api(apiBase());state.job=result.job||result;
      await pollIndex(true);
    }catch(error){showIndex("failed",errorMessage(error));$("build-index").hidden=true;}
  }
  init();
})();
