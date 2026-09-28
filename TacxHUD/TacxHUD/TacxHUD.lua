-- TacxHUD.lua
-- Client-side addon: displays live cycling stats in a WoW-styled panel.
-- Data arrives from the server via an addon message (prefix "TACX"),
-- sent by speedbridge.lua, which relays it from the Python bridge.
--
-- Drag the panel by its title bar to reposition it; position is not
-- saved between sessions in this first version.

local frame = CreateFrame("Frame", "TacxHUDFrame", UIParent, "BackdropTemplate")
frame:SetSize(220, 170)
frame:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 20, -200)
frame:SetBackdrop({
    bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
    edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
    tile = true, tileSize = 32, edgeSize = 32,
    insets = { left = 11, right = 12, top = 12, bottom = 11 },
})

frame:SetMovable(true)
frame:EnableMouse(true)
frame:RegisterForDrag("LeftButton")
frame:SetScript("OnDragStart", function(self) self:StartMoving() end)
frame:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)

local title = frame:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
title:SetPoint("TOP", frame, "TOP", 0, -16)
title:SetText("Tacx Ride")

local function CreateStatLine(labelText, yOffset)
    local label = frame:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    label:SetPoint("TOPLEFT", frame, "TOPLEFT", 24, yOffset)
    label:SetText(labelText)

    local value = frame:CreateFontString(nil, "OVERLAY", "GameFontHighlightLarge")
    value:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -24, yOffset)
    value:SetText("--")

    return value
end

local speedValue = CreateStatLine("Speed (km/h)", -50)
local powerValue = CreateStatLine("Power (W)", -74)
local distanceValue = CreateStatLine("Distance (m)", -98)
local toGoValue = CreateStatLine("To Go (m)", -122)
local elevationValue = CreateStatLine("Gradient", -146)

local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("CHAT_MSG_ADDON")
eventFrame:SetScript("OnEvent", function(self, event, prefix, message, channel, sender)
    if prefix ~= "TACX" then
        return
    end

    local speed, power, distance, toGo, elevation = message:match("([^,]+),([^,]+),([^,]+),([^,]+),([^,]+)")
    if not speed then
        return
    end

    speedValue:SetText(speed)
    powerValue:SetText(power)
    distanceValue:SetText(distance)
    toGoValue:SetText(toGo)

    local gradeNum = tonumber(elevation)
    if gradeNum and gradeNum > 0 then
        elevationValue:SetText("|cffff4040" .. elevation .. "%|r")  -- red for climbing
    elseif gradeNum and gradeNum < 0 then
        elevationValue:SetText("|cff40ff40" .. elevation .. "%|r")  -- green for descending
    else
        elevationValue:SetText((elevation or "0.0") .. "%")
    end
end)

print("TacxHUD loaded. Drag the panel by its top area to reposition.")
