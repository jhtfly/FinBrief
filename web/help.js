/* v2.13.5 使用说明书浮窗：README.md → 简易 Markdown 渲染 + 左侧目录
   可拖动 / 缩放（CSS resize）/ 最大化 / 最小化，不遮主界面（非模态）。 */
(function(){
  "use strict";

  function esc(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function inlineFmt(t){
    // v2.13.6 白名单 <b>/<code>：先占位防 esc 转义
    const safe = [];
    t = (t || "").replace(/<(b|code)>(.*?)<\/>/g, (m0) => {
      safe.push(m0); return "" + (safe.length - 1) + "";
    });
    t = esc(t);
    t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a class="link" href="$2" target="_blank">$1</a>');
    t = t.replace(/(\d+)/g, (m0, i) => safe[+i] || "");
    return t;
  }

  /* 极简 Markdown：#/##/### 标题、- 列表、表格、粗体/代码/链接、普通段落 */
  function miniMd(src){
    const lines = (src || "").replace(/\r/g, "").split("\n");
    const blocks = [];
    let sec = 0, toc = [];
    let i = 0;
    let listBuf = null, tableBuf = null, paraBuf = null;
    const flush = () => {
      if(listBuf){ blocks.push("<ul>" + listBuf.map(x => "<li>" + x + "</li>").join("") + "</ul>"); listBuf = null; }
      if(tableBuf){ blocks.push('<table style="width:100%;border-collapse:collapse">' +
        tableBuf.map((row, ri) => "<tr>" + row.map(c =>
          (ri === 0 ? `<th style="padding:5px 8px;border-bottom:2px solid currentColor;text-align:left">` :
            `<td style="padding:5px 8px;border-bottom:1px solid rgba(128,128,128,.25);vertical-align:top">`) + c + (ri===0?"</th>":"</td>")
        ).join("") + "</tr>").join("") + "</table>"); tableBuf = null; }
      if(paraBuf){ blocks.push("<div style=\"margin:7px 0;line-height:1.75\">" + paraBuf.join("<br>") + "</div>"); paraBuf = null; }
    };
    for(; i < lines.length; i++){
      const ln = lines[i];
      if(/^\s*$/.test(ln)){ flush(); continue; }
      const h = /^(#{1,4})\s+(.*)$/.exec(ln);
      if(h){
        flush();
        sec++;
        const lvl = h[1].length;
        const tag = lvl <= 2 ? "h2" : "h3";
        const anchor = `sec${sec}`;
        toc.push({ lvl, text: h[2].replace(/\*\*/g, ""), anchor });
        blocks.push(`<${tag} id="${anchor}" class="md-head" style="margin:14px 0 8px">${inlineFmt(h[2])}</${tag}>`);
        continue;
      }
      if(/^\s*\|.*\|\s*$/.test(ln)){
        if(/^\s*\|[\s:\-|]+\|\s*$/.test(ln)) continue;   // 分隔行
        if(!tableBuf){ flush(); tableBuf = []; }
        tableBuf.push(ln.trim().replace(/^\||\|$/g, "").split("|").map(c => inlineFmt(c.trim())));
        continue;
      }
      if(/^\s*[-*]\s+/.test(ln)){
        if(tableBuf || paraBuf) flush();
        if(!listBuf) listBuf = [];
        listBuf.push(inlineFmt(ln.replace(/^\s*[-*]\s+/, "")));
        continue;
      }
      if(/^\s*\d+\.\s+/.test(ln)){
        if(tableBuf || paraBuf) flush();
        if(!listBuf) listBuf = [];
        // v2.13.6 修：bullet 标签在 inlineFmt 之后注入（此前被 esc 转义成字面显示）
        listBuf.push("<b>•</b> " + inlineFmt(ln.replace(/^\s*\d+\.\s+/, "")));
        continue;
      }
      if(paraBuf === null && !tableBuf && !listBuf){ paraBuf = []; }
      paraBuf.push(inlineFmt(ln));
    }
    flush();
    return { html: blocks.join("\n"), toc };
  }

  let _built = false;
  window.openUserGuide = function(){
    const bg = $("#helpBg");
    if(!bg) return;
    bg.classList.add("on");
    bg.style.display = "flex";
    const dock = $("#helpDock");
    if(dock) dock.style.display = "none";
    if(!_built){
      _built = true;
      const body = $("#helpContent");
      if(body) body.innerHTML = '<div class="muted" style="padding:30px">正在载入使用说明…</div>';
      api("/api/help_doc").then(r => {
        if(!r || !r.ok){ if(body) body.innerHTML = '<div class="muted" style="padding:30px">' + esc((r && r.text) || "未能读取使用说明。") + "</div>"; return; }
        const m = miniMd(r.text);
        $("#helpContent").innerHTML = m.html;
        const tocBox = $("#helpToc");
        tocBox.innerHTML = m.toc.map(t =>
          `<div style="padding:3px 6px;cursor:pointer;border-radius:6px;font-size:12.5px;${t.lvl<=2?"font-weight:700":""}"
             onmouseover="this.style.background='rgba(128,128,128,.12)'"
             onmouseout="this.style.background=''" onclick="helpJump('${t.anchor}')">${esc(t.text)}</div>`).join("");
      }).catch(e => {
        if(body) body.innerHTML = '<div class="muted" style="padding:30px">载入失败：' + esc(String(e).slice(0, 90)) + "</div>";
      });
    }
  };
  window.helpJump = function(anchor){
    const el = document.getElementById(anchor);
    if(el) el.scrollIntoView({ block: "start", behavior: "smooth" });
  };

  function initHelp(){
    const bg = $("#helpBg");
    if(!bg) return;
    const title = bg.querySelector(".fw-title");
    let sx = 0, sy = 0, bx = 0, by = 0, dragging = false;
    title.addEventListener("mousedown", e => {
      if(e.target.tagName === "BUTTON") return;
      const r = bg.getBoundingClientRect();
      if(bg.classList.contains("max")) return;
      dragging = true; sx = e.clientX; sy = e.clientY; bx = r.left; by = r.top;
      bg.style.transition = "none";
      e.preventDefault();
    });
    document.addEventListener("mousemove", e => {
      if(!dragging) return;
      const nx = Math.max(8, Math.min(window.innerWidth - 120, bx + e.clientX - sx));
      const ny = Math.max(8, Math.min(window.innerHeight - 60, by + e.clientY - sy));
      bg.style.left = nx + "px"; bg.style.top = ny + "px";
      bg.style.right = "auto";
    });
    document.addEventListener("mouseup", () => { dragging = false; });

    const maxBtn = $("#helpMax"), minBtn = $("#helpMin"), clsBtn = $("#helpCls");
    if(maxBtn) maxBtn.onclick = () => {
      bg.classList.toggle("max");
      maxBtn.textContent = bg.classList.contains("max") ? "❐" : "□";
    };
    if(clsBtn) clsBtn.onclick = () => {
      bg.classList.remove("on"); bg.style.display = "none";
      const dock = $("#helpDock"); if(dock) dock.style.display = "none";
    };
    if(minBtn) minBtn.onclick = () => {
      bg.classList.remove("on"); bg.style.display = "none";
      const dock = $("#helpDock"); if(dock) dock.style.display = "flex";
    };
    const dock = $("#helpDock");
    if(dock) dock.onclick = () => {
      bg.classList.add("on"); bg.style.display = "flex";
      dock.style.display = "none";
    };
  }
  if(document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", initHelp);
  } else {
    initHelp();
  }
})();
