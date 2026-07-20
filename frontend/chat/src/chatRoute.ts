const NEW_CHAT_PATH = "/";

export function sessionFromPath(pathname: string): string {
  const match = pathname.match(/^\/chat\/([^/]+)\/?$/);
  if (!match) return "";
  const id = decodeURIComponent(match[1]).trim();
  return id ? (id.startsWith("web:") ? id : `web:${id}`) : "";
}

export function pathForSession(sessionId: string): string {
  const id = sessionId.replace(/^web:/, "");
  return id ? `/chat/${encodeURIComponent(id)}` : NEW_CHAT_PATH;
}

export function routeKey(sessionId: string): string {
  return sessionId || "__new__";
}
