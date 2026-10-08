/* Dependency-free Markdown for message bodies. Raw HTML is always text.
 * This intentionally supports a small, safe subset rather than embedded HTML,
 * images, or arbitrary URL schemes. Incomplete streamed blocks remain readable.
 */
(function (root) {
  "use strict";
  const escape = value => String(value).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  function safeURL(value) {
    if (!value || /[\s\u0000-\u001f\u007f\\]/.test(value)) return false;
    if (/^agentcoord:/i.test(value)) {
      const navigation = typeof module !== "undefined" && module.exports ? require("./navigation.js") : root.agentCoordNavigation;
      return navigation?.safe(value) || false;
    }
    return /^(https?:\/\/|mailto:)/i.test(value) || (!/^[a-z][a-z\d+.-]*:/i.test(value) && !value.startsWith("//"));
  }
  function inline(text, depth = 0) {
    if (depth > 16) return escape(text);
    let out = "", i = 0;
    while (i < text.length) {
      const rest = text.slice(i);
      let match;
      if ((match = /^\\([\\`*_{}\[\]()#+.!>~|\-])/.exec(rest))) {
        out += escape(match[1]); i += match[0].length; continue;
      }
      if ((match = /^(`+)([\s\S]*?[^`])\1(?!`)/.exec(rest))) {
        out += "<code>" + escape(match[2].replace(/\n/g, " ")) + "</code>"; i += match[0].length; continue;
      }
      if ((match = /^\[([^\]\n]+)\]\((<[^>\n]+>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)(?:\s+"[^"\n]*")?\)/.exec(rest))) {
        const url = match[2].replace(/^<|>$/g, "");
        // Labels cannot contain links of their own.
        const label = inline(match[1], depth + 1);
        out += safeURL(url) ? '<a href="' + escape(url) + '" target="_blank" rel="noopener noreferrer">' + label + "</a>" : label;
        i += match[0].length; continue;
      }
      if ((match = /^<(https?:\/\/[^<>\s]+|mailto:[^<>\s]+)>/.exec(rest)) && safeURL(match[1])) {
        out += '<a href="' + escape(match[1]) + '" target="_blank" rel="noopener noreferrer">' + escape(match[1]) + "</a>";
        i += match[0].length; continue;
      }
      let formatted = false;
      for (const [mark, tag] of [["***", "strong"], ["**", "strong"], ["__", "strong"], ["~~", "del"], ["*", "em"], ["_", "em"]]) {
        if (!rest.startsWith(mark) || /\s/.test(rest[mark.length] || " ")) continue;
        if (mark.includes("_") && /[\w]/.test(text[i - 1] || "")) continue;
        const end = text.indexOf(mark, i + mark.length);
        if (end <= i + mark.length || /\s/.test(text[end - 1])) continue;
        const body = inline(text.slice(i + mark.length, end), depth + 1);
        out += "<" + tag + ">" + (mark === "***" ? "<em>" + body + "</em>" : body) + "</" + tag + ">";
        i = end + mark.length; formatted = true; break;
      }
      if (formatted) continue;
      if (text[i] === "\n") out += "<br>\n";
      else out += escape(text[i]);
      i++;
    }
    return out;
  }
  const listItem = line => /^(\s*)([-+*]|\d+[.)])\s+(.*)$/.exec(line);
  const fence = line => /^\s{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
  const rule = line => /^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line);
  function cells(line) {
    return line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/).map(cell => cell.trim());
  }
  function tableRule(line) {
    const columns = cells(line);
    return line.includes("|") && columns.every(cell => /^:?-{3,}:?$/.test(cell)) ? columns : null;
  }
  function blocks(lines, depth = 0) {
    if (depth > 16) return "<p>" + inline(lines.join("\n")) + "</p>";
    let out = "", i = 0;
    const startsBlock = line => fence(line) || /^\s{0,3}#{1,6}\s/.test(line) || /^\s{0,3}>/.test(line) || listItem(line) || rule(line);
    while (i < lines.length) {
      const line = lines[i];
      let match;
      if (!line.trim()) { i++; continue; }
      if ((match = fence(line))) {
        const marker = match[1], code = [];
        i++;
        while (i < lines.length && !new RegExp("^\\s{0,3}" + marker[0] + "{" + marker.length + ",}\\s*$").test(lines[i])) code.push(lines[i++]);
        if (i < lines.length) i++;
        out += "<pre><code>" + escape(code.join("\n")) + "</code></pre>"; continue;
      }
      if ((match = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line))) {
        const tag = "h" + match[1].length;
        out += "<" + tag + ">" + inline(match[2]) + "</" + tag + ">"; i++; continue;
      }
      if (rule(line)) { out += "<hr>"; i++; continue; }
      if (/^\s{0,3}>/.test(line)) {
        const quote = [];
        while (i < lines.length && /^\s{0,3}>/.test(lines[i])) quote.push(lines[i++].replace(/^\s{0,3}> ?/, ""));
        out += "<blockquote>" + blocks(quote, depth + 1) + "</blockquote>"; continue;
      }
      if ((match = listItem(line))) {
        const indent = match[1].length, ordered = /^\d/.test(match[2]), tag = ordered ? "ol" : "ul";
        out += "<" + tag + (ordered ? ' start="' + Number.parseInt(match[2], 10) + '"' : "") + ">";
        while (i < lines.length) {
          const item = listItem(lines[i]);
          if (!item || item[1].length !== indent || /^\d/.test(item[2]) !== ordered) break;
          const body = [item[3]], contentIndent = item[0].length - item[3].length;
          i++;
          while (i < lines.length) {
            if (!lines[i].trim()) {
              if (i + 1 < lines.length && /^\s+\S/.test(lines[i + 1]) && lines[i + 1].search(/\S/) > indent) { body.push(""); i++; continue; }
              break;
            }
            if (lines[i].search(/\S/) <= indent) break;
            body.push(lines[i].slice(Math.min(contentIndent, lines[i].search(/\S/))));
            i++;
          }
          const task = /^\[([ xX])\]\s+/.exec(body[0]);
          if (task) body[0] = body[0].slice(task[0].length);
          out += "<li>" + (task ? '<input type="checkbox" disabled aria-label="Task"' + (task[1] !== " " ? " checked" : "") + "> " : "") + blocks(body, depth + 1) + "</li>";
          while (i < lines.length && !lines[i].trim()) i++;
        }
        out += "</" + tag + ">"; continue;
      }
      const columns = i + 1 < lines.length && tableRule(lines[i + 1]);
      if (line.includes("|") && columns && cells(line).length === columns.length) {
        const row = (values, tag) => "<tr>" + columns.map((column, n) => {
          const align = column.startsWith(":") ? (column.endsWith(":") ? "center" : "left") : column.endsWith(":") ? "right" : "left";
          return "<" + tag + ' style="text-align:' + align + '">' + inline(values[n] || "") + "</" + tag + ">";
        }).join("") + "</tr>";
        out += '<div class="markdown-table"><table><thead>' + row(cells(line), "th") + "</thead><tbody>";
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) out += row(cells(lines[i++]), "td");
        out += "</tbody></table></div>"; continue;
      }
      const paragraph = [line]; i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) {
        if (i + 1 < lines.length && lines[i].includes("|") && tableRule(lines[i + 1])) break;
        paragraph.push(lines[i++]);
      }
      out += "<p>" + inline(paragraph.join("\n")) + "</p>";
    }
    return out;
  }
  const api = {render: text => blocks(String(text ?? "").replace(/\r\n?/g, "\n").split("\n"))};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.messageMarkdown = api;
})(globalThis);
