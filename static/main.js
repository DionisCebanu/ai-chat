const el = {
  messages: document.getElementById("messages"),
  form: document.getElementById("form"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  status: document.getElementById("status"),
};

let pending = false;
let typingNode = null;

function addMessage(kind, text, meta) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + kind;

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (kind === "bot") {
    const lines = String(text || "").split(/\r?\n/);
    let hadContent = false;

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;

      const url = getImageUrlFromLine(line);
      if (url) {
        bubble.appendChild(createImageEl(url));
        hadContent = true;
        continue;
      }

      const p = document.createElement("p");
      p.innerHTML = linkify(line); // assumes linkify() is defined
      bubble.appendChild(p);
      hadContent = true;
    }

    if (!hadContent) {
      bubble.innerHTML = linkify(text || "");
    }
  } else {
    bubble.textContent = text || ""; // safe for user text
  }

  const metaDiv = document.createElement("div");
  metaDiv.className = "meta";
  metaDiv.textContent = meta || (kind === "user" ? "You" : "Bot");
  bubble.appendChild(metaDiv);

  if (kind === "bot") {
    const avatar = document.createElement("div");
    avatar.className = "avatar bot";
    avatar.textContent = "🤖";
    wrap.appendChild(avatar);
    wrap.appendChild(bubble);
  } else {
    wrap.appendChild(bubble);
    const avatar = document.createElement("div");
    avatar.className = "avatar user";
    avatar.textContent = "Y";
    wrap.appendChild(avatar);
  }

  el.messages.appendChild(wrap);
  el.messages.scrollTop = el.messages.scrollHeight;
  return wrap;
}


function showTyping() {
  const wrap = document.createElement("div");
  wrap.className = "msg bot";
  const avatar = document.createElement("div");
  avatar.className = "avatar bot";
  avatar.textContent = "🤖";
  const bubble = document.createElement("div");
  bubble.className = "bubble typing";
  bubble.setAttribute("aria-label", "Bot is typing");
  bubble.innerHTML = "<span></span><span></span><span></span>";
  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  el.messages.appendChild(wrap);
  el.messages.scrollTop = el.messages.scrollHeight;
  typingNode = wrap;
}

function hideTyping() {
  if (typingNode && typingNode.parentNode) typingNode.parentNode.removeChild(typingNode);
  typingNode = null;
}

function setPending(v) {
  pending = v;
  el.input.disabled = v;
  el.send.disabled = v;
}

async function sendMessage(text) {
  if (!text || pending) return;
  setPending(true);
  addMessage("user", text);

  showTyping();
  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const json = await res.json();
    hideTyping();

    if (!res.ok) {
      addMessage("bot", json.error || "Something went wrong.");
    } else {
      addMessage("bot", json.reply || "(no reply)");
    }
  } catch (e) {
    hideTyping();
    addMessage("bot", "Cannot reach /chat. Is the server running?");
    el.status.textContent = "• offline";
  } finally {
    setPending(false);
  }
}


// Helper: return image URL if the line is an IMG directive, else null.
function getImageUrlFromLine(line) {
  const m = String(line || "").trim().match(/^IMG:\s*(https?:\/\/\S+)/i);
  return m ? m[1] : null;
}

// Helper: build a styled <img> element.
function createImageEl(url) {
  const img = document.createElement("img");
  img.src = url;
  img.alt = "image result";
  img.loading = "lazy";
  img.referrerPolicy = "no-referrer";
  img.style.maxWidth = "100%";
  img.style.borderRadius = "10px";
  img.style.margin = "6px 0";
  return img;
}

// --- Safe HTML helpers ---
function escapeHTML(str) {
  return (str ?? "").replace(/[&<>"']/g, m =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[m])
  );
}

// Transforme les URLs en liens cliquables (tout le reste est échappé)
function linkify(text) {
  const escaped = escapeHTML(text);
  const urlRe = /(https?:\/\/[^\s<>()]+[^\s<>().,!?;:'")\]])/g;
  return escaped.replace(urlRe, (url) =>
    `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
  );
}

el.form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = el.input.value.trim();
  el.input.value = "";
  sendMessage(text);
});

el.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    el.form.dispatchEvent(new Event("submit"));
  }
});
