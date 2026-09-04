import { describe, expect, it } from "vitest";

import { isModelSettingsPath, MODEL_SETTINGS_PATH, pathForSession, routeKey, sessionFromPath } from "./chatRoute";

describe("chatRoute", () => {
  it("在新会话和具体会话 URL 之间双向转换", () => {
    expect(sessionFromPath("/")).toBe("");
    expect(sessionFromPath("/chat/abc123")).toBe("web:abc123");
    expect(pathForSession("web:abc123")).toBe("/chat/abc123");
  });

  it("为新会话和具体会话提供独立草稿键", () => {
    expect(routeKey("")).toBe("__new__");
    expect(routeKey("web:abc123")).toBe("web:abc123");
  });

  it("只把模型设置路径识别为独立设置页", () => {
    expect(MODEL_SETTINGS_PATH).toBe("/settings/models");
    expect(isModelSettingsPath("/settings/models")).toBe(true);
    expect(isModelSettingsPath("/settings/models/")).toBe(true);
    expect(isModelSettingsPath("/chat/settings")).toBe(false);
  });
});
