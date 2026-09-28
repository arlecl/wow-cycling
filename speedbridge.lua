-- speedbridge.lua
-- Reads a target speed "rate" (1 = normal) from a shared file every second
-- and applies it to the player, using the same .modify speed approach we
-- already confirmed works manually.
--
-- Also supports route recording: type "startroute" then walk a real road,
-- type "stoproute" when done. Position gets logged automatically every
-- second to a file, ready to be converted into a route file.

local PLAYER_EVENT_ON_LOGIN = 3
local PLAYER_EVENT_ON_CHAT = 18
local MY_CHARACTER_NAME = "Shamishaman"  -- IMPORTANT: only this character triggers our logic,
                                          -- since Playerbots' bots are real Player objects too
                                          -- and would otherwise trigger these handlers as well
local SPEED_FILE_PATH = "/tmp/tacx_speed_target.txt"
local RECORDING_FILE_PATH = "/tmp/tacx_route_recording.txt"
local LIVE_POSITION_FILE_PATH = "/tmp/tacx_live_position.txt"
local TELEPORT_FILE_PATH = "/tmp/tacx_teleport_target.txt"
local COMMAND_FILE_PATH = "/tmp/tacx_command.txt"
local RESPONSE_FILE_PATH = "/tmp/tacx_response.txt"
local HUD_DATA_FILE_PATH = "/tmp/tacx_hud_data.txt"
local CHECK_INTERVAL_MS = 1000
local CHANGE_THRESHOLD = 0.05

local lastSeenResponse = nil

local lastAppliedRate = {}
local recordingState = {}  -- keyed by GUID: true/false/nil

local function ReadTargetRate()
    local f = io.open(SPEED_FILE_PATH, "r")
    if not f then
        return nil
    end
    local content = f:read("*a")
    f:close()
    return tonumber(content)
end

local function CheckSpeedFile(eventId, delay, repeats, player)
    if not player or not player:IsInWorld() then
        return
    end
    if player:GetName() ~= MY_CHARACTER_NAME then
        return
    end

    local guid = player:GetGUIDLow()

    -- ALWAYS report live position FIRST, before anything else that could
    -- fail -- steering depends on this every tick, so it must never be
    -- skipped due to an error elsewhere in this function.
    do
        local x, y, z, o = player:GetX(), player:GetY(), player:GetZ(), player:GetO()
        local mapId = player:GetMapId()
        local f = io.open(LIVE_POSITION_FILE_PATH, "w")
        if f then
            f:write(string.format("%d,%.3f,%.3f,%.3f,%.3f\n", mapId, x, y, z, o))
            f:close()
        end
    end

    -- One-time teleport to route start, if requested.
    do
        local f = io.open(TELEPORT_FILE_PATH, "r")
        if f then
            local content = f:read("*a")
            f:close()
            local tx, ty, tz, tfacing = content:match("([^,]+),([^,]+),([^,]+),([^,]+)")
            if tx then
                player:NearTeleport(tonumber(tx), tonumber(ty), tonumber(tz), tonumber(tfacing))
                player:SendBroadcastMessage("Teleported to route start.")
            end
            os.remove(TELEPORT_FILE_PATH)
        end
    end

    local rate = ReadTargetRate()
    if rate then
        local last = lastAppliedRate[guid]
        if not last or math.abs(rate - last) >= CHANGE_THRESHOLD then
            player:RunCommand(".modify speed " .. string.format("%.2f", rate))
            lastAppliedRate[guid] = rate
        end
    end

    -- Print any new response from Python (e.g. route list), once.
    pcall(function()
        local f = io.open(RESPONSE_FILE_PATH, "r")
        if f then
            local content = f:read("*a")
            f:close()
            if content and content ~= "" and content ~= lastSeenResponse then
                lastSeenResponse = content
                for line in content:gmatch("[^\n]+") do
                    player:SendBroadcastMessage(line)
                end
            end
        end
    end)

    -- Relay live ride stats to the client-side HUD addon.
    pcall(function()
        local CHAT_MSG_WHISPER = 7  -- ChatMsg enum value; adjust here if HUD stays blank
        local f = io.open(HUD_DATA_FILE_PATH, "r")
        if f then
            local content = f:read("*a")
            f:close()
            if content and content ~= "" then
                player:SendAddonMessage("TACX", content, CHAT_MSG_WHISPER, player)
            end
        end
    end)

    -- If recording is active for this player, also log to the recording file.
    pcall(function()
        if recordingState[guid] then
            local x, y, z, o = player:GetX(), player:GetY(), player:GetZ(), player:GetO()
            local mapId = player:GetMapId()
            local f = io.open(RECORDING_FILE_PATH, "a")
            if f then
                f:write(string.format("%d,%.3f,%.3f,%.3f,%.3f\n", mapId, x, y, z, o))
                f:close()
            end
        end
    end)
end

local function WriteCommand(cmd)
    local f = io.open(COMMAND_FILE_PATH, "w")
    if f then
        f:write(cmd)
        f:close()
    end
end

local function OnChat(event, player, msg, Type, lang)
    if player:GetName() ~= MY_CHARACTER_NAME then
        return
    end
    local guid = player:GetGUIDLow()

    if msg == "startroute" then
        -- Start fresh: clear any previous recording file
        local f = io.open(RECORDING_FILE_PATH, "w")
        if f then f:close() end
        recordingState[guid] = true
        player:SendBroadcastMessage("Recording started. Walk your route now. Type 'stoproute' when done.")

    elseif msg == "stoproute" then
        recordingState[guid] = false
        player:SendBroadcastMessage("Recording stopped. Saved to " .. RECORDING_FILE_PATH)

    elseif msg == "routes" then
        WriteCommand("LIST_ROUTES")
        player:SendBroadcastMessage("Checking available routes...")

    elseif msg:match("^startride ") then
        local routeName = msg:match("^startride (.+)$")
        WriteCommand("START_ROUTE:" .. routeName)
        player:SendBroadcastMessage("Starting route: " .. routeName)

    elseif msg == "stopride" then
        WriteCommand("STOP_RIDE")
        player:SendBroadcastMessage("Stopping ride.")

    elseif msg == "snap" then
        WriteCommand("SNAP")
        player:SendBroadcastMessage("Taking screenshot...")

    elseif msg:match("^saveroute ") then
        local routeName = msg:match("^saveroute (.+)$")
        WriteCommand("SAVE_ROUTE:" .. routeName)
        player:SendBroadcastMessage("Saving recording as route: " .. routeName)

    elseif msg == "help" then
        player:SendBroadcastMessage("--- Tacx Bridge Commands ---")
        player:SendBroadcastMessage("routes            - list available routes")
        player:SendBroadcastMessage("startride <name>  - teleport to route start and begin riding")
        player:SendBroadcastMessage("stopride          - stop the current ride")
        player:SendBroadcastMessage("startroute        - begin recording a new route (walk after this)")
        player:SendBroadcastMessage("stoproute         - stop recording")
        player:SendBroadcastMessage("saveroute <name>  - save the last recording as a usable route")
        player:SendBroadcastMessage("snap              - take a screenshot now")
        player:SendBroadcastMessage("help              - show this list")
    end
end

local function OnLogin(event, player)
    if player:GetName() ~= MY_CHARACTER_NAME then
        return
    end
    player:SendBroadcastMessage("Speed bridge active. Type 'help' for commands.")
    player:RegisterEvent(CheckSpeedFile, CHECK_INTERVAL_MS, 0)
end

RegisterPlayerEvent(PLAYER_EVENT_ON_LOGIN, OnLogin)
RegisterPlayerEvent(PLAYER_EVENT_ON_CHAT, OnChat)

print("speedbridge.lua loaded successfully.")
