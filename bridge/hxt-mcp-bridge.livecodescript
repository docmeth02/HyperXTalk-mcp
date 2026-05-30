script "hxt-mcp-bridge"

-- HyperXTalk MCP bridge (logic). Hosted as an IDE plugin, launched once per session.
-- This is the stack script; build-plugin.livecodescript wraps it into a binary plugin stack.
-- Phases 1-4 run it invisibly; phase 5 adds a binary palette UI shell.
--
-- Phase 1 (this file): custom loopback /rpc reader (async read ... with message so a slow client
-- never freezes the IDE), handshake in the per-OS user data folder (== platformdirs.user_data_dir;
-- macOS ~/Library/Application Support/HyperXTalk, stable across GUI/CLI, not "temporary"), ops
-- ping/echo, idle|busy state machine, closeStack cleanup. Transport ingest is bespoke (NOT the
-- httpd library, which truncates large bodies); the json library does both directions
-- (JsonImport parses requests, JsonExport builds responses — O(n), RFC-compliant escaping).
--
-- All OS-specific paths via specialFolderPath(). Var prefixes: t=local p=param s=script-local k=const.

constant kBridgeVersion = "0.0.1"
constant kHandshakeName = "mcp-bridge.json"
constant kMaxBody = 67108864      -- 64 MB request-body cap (OOM guard)
constant kSocketTimeout = 30000   -- ms; releases hung connections (frees the busy token)
constant kCreatableTypes = "button,field,graphic,image,scrollbar,player,group"
constant kLogMax = 500            -- max lines kept in the palette's Log tab

local sListenPort        -- the listening (server) socket id == its port; empty when stopped
local sToken             -- per-session shared secret
local sState             -- "idle" | "busy" (strict one-in-flight)
local sBusySocket        -- the connection socket currently holding the busy token
local sConn              -- per-connection scratch: sConn[socket]["token"|"method"|"path"|"contentLength"]
local sPaused            -- master "pause edits" switch (panel/tool); blocks mutations when true
local sKeepAliveID       -- pending keepAlive message id, so we can cancel it cleanly
local sLastCreatedId     -- numeric id of the just-created control (captured inside the create `do`)
local sAllowDo           -- escape-hatch (do/value) opt-in; off by default (gates a phase-8 op)
local sLog               -- ring buffer of recent request lines (for the Log tab)


-- ============================================================ lifecycle

on openStack
   hxtBridgeStart
   hxtRefreshStatus
end openStack

on closeStack
   hxtBridgeStop
end closeStack

-- Public so it can be started manually from the message box during development.
-- Returns empty on success, or an error string. Also logs to ~/hxt-mcp-bridge.log either way,
-- because `dispatch` (used by some callers) discards the return value.
command hxtBridgeStart
   global gHxtMcpBridgeOwner
   if sListenPort is not empty then return "already running on port" && sListenPort
   -- Singleton guard: a second instance must not start and overwrite the shared handshake file.
   -- But clear a STALE owner (a stack id that no longer exists) so we never lock ourselves out.
   if gHxtMcpBridgeOwner is not empty and gHxtMcpBridgeOwner is not (the long id of me) then
      if there is a stack (gHxtMcpBridgeOwner) then
         hxtLog "start refused: bridge already owned by" && gHxtMcpBridgeOwner
         return "bridge already owned by another instance"
      end if
      hxtLog "clearing stale bridge owner:" && gHxtMcpBridgeOwner
      put empty into gHxtMcpBridgeOwner
   end if
   put the long id of me into gHxtMcpBridgeOwner
   try
      -- ROOT-CAUSE FIX: stop the engine from purging this invisible stack. When the owner object is
      -- destroyed, IO_freeobject (mcio.cpp) closes its sockets WITHOUT firing closeStack, killing the
      -- listener with the handshake still present. destroyStack=false keeps the stack resident.
      set the destroyStack of me to false
      hxtLog "start: platform=" & the platform & ", folder=" & hxtHandshakeFolder()
      put "idle" into sState
      put empty into sBusySocket
      put false into sPaused
      put false into sAllowDo
      put empty into sLog
      put hxtMakeToken() into sToken
      set the socketTimeoutInterval to kSocketTimeout

      -- Bind the listener to loopback only (accept binds all interfaces unless this is set first).
      local tOldIface, tErr
      put the defaultNetworkInterface into tOldIface
      set the defaultNetworkInterface to "127.0.0.1"
      accept connections on port 0 with message "__HxtAccept"
      put the result into tErr
      put it into sListenPort
      set the defaultNetworkInterface to tOldIface

      if tErr is not empty then
         put empty into sListenPort
         hxtLog "accept failed:" && tErr
         return "could not open socket:" && tErr
      end if
      hxtLog "listening on port" && sListenPort

      local tHsErr
      put hxtWriteHandshake() into tHsErr
      if tHsErr is not empty then
         hxtLog "handshake error:" && tHsErr
         hxtBridgeStop
         return "handshake write failed:" && tHsErr
      end if
      hxtLog "ready: handshake at" && hxtHandshakePath()
      send "hxtKeepAlive" to me in 30 seconds   -- keeps a pending ref + drives the watchdog
      put the result into sKeepAliveID
      hxtRefreshStatus
      return empty
   catch tException
      -- don't leave the IDE's network interface pinned to loopback if accept threw mid-way
      set the defaultNetworkInterface to empty
      hxtLog "EXCEPTION in hxtBridgeStart:" && tException
      return "exception:" && tException
   end try
end hxtBridgeStart

command hxtBridgeStop
   global gHxtMcpBridgeOwner
   hxtLog "bridgeStop called (port=" & sListenPort & ")"
   -- Cancel ALL pending keepAlive timers (the captured id can be unreliable), so a rebuild truly
   -- stops the old instance instead of leaving a duplicate running.
   local tMsg
   repeat for each line tMsg in the pendingMessages
      -- match by substring (the target's long id can contain commas, shifting item positions)
      if tMsg contains "hxtKeepAlive" then cancel (item 1 of tMsg)
   end repeat
   put empty into sKeepAliveID
   if gHxtMcpBridgeOwner is (the long id of me) then put empty into gHxtMcpBridgeOwner
   if sListenPort is not empty then
      if sListenPort is among the lines of the openSockets then close socket sListenPort
   end if
   -- Close any in-flight connection sockets too, then drop all per-connection state.
   local tSocket
   repeat for each key tSocket in sConn
      if tSocket is among the lines of the openSockets then close socket tSocket
   end repeat
   delete variable sConn
   hxtDeleteHandshake
   put empty into sListenPort
   put empty into sToken
   put "idle" into sState
   put empty into sBusySocket
   hxtRefreshStatus
end hxtBridgeStop

function hxtBridgeStatus
   return "port:" && sListenPort & comma & "state:" && sState
end hxtBridgeStatus

-- Self-rescheduling timer: keeps the invisible stack referenced (so the engine doesn't purge it)
-- and logs listener liveness each cycle for diagnosis.
on hxtKeepAlive
   if sListenPort is empty then exit hxtKeepAlive   -- bridge stopped; let the timer lapse
   -- If the listener vanished, just clear our stale handshake and stop. Do NOT re-accept on a new
   -- port (that caused handshake churn / refusals). destroyStack=false is the real keep-alive.
   if sListenPort is not among the lines of the openSockets then
      hxtLog "listener missing; clearing stale handshake (no re-accept)"
      hxtDeleteHandshake
      put empty into sListenPort
      exit hxtKeepAlive
   end if
   send "hxtKeepAlive" to me in 30 seconds
   put the result into sKeepAliveID
end hxtKeepAlive


-- ============================================================ palette UI (phase 5)

-- (Re)build the control-panel UI on this stack. Called once by build-plugin; idempotent.
command hxtBuildUI
   -- `create` targets the defaultStack, so point it at us during the build, then restore it.
   local tOldDefault
   put the defaultStack into tOldDefault
   set the defaultStack to the long id of me
   set the width of me to 440
   set the height of me to 340
   set the title of me to "HyperXTalk MCP Bridge"

   local tNames, tN
   put "hxtTabs,hxtLight,hxtStatusText,hxtPause,hxtEscape,hxtTokenLabel,hxtToken,hxtLog" into tNames
   repeat for each item tN in tNames
      if there is a control tN of card 1 of me then delete control tN of card 1 of me
   end repeat

   -- tabbed frame
   create button "hxtTabs"
   set the style of button "hxtTabs" to "menu"
   set the menuMode of button "hxtTabs" to "tabbed"
   set the showName of button "hxtTabs" to false
   set the text of button "hxtTabs" to "Status" & return & "Settings" & return & "Log"
   set the rect of button "hxtTabs" to 8,8,432,332

   -- Status tab
   create graphic "hxtLight"
   set the style of graphic "hxtLight" to "oval"
   set the opaque of graphic "hxtLight" to true
   set the lineSize of graphic "hxtLight" to 0
   set the rect of graphic "hxtLight" to 22,56,40,74
   create field "hxtStatusText"
   set the lockText of field "hxtStatusText" to true
   set the showBorder of field "hxtStatusText" to false
   set the opaque of field "hxtStatusText" to false
   set the rect of field "hxtStatusText" to 48,52,420,76

   -- Settings tab
   create button "hxtPause"
   set the style of button "hxtPause" to "checkbox"
   set the label of button "hxtPause" to "Pause edits (block all mutations)"
   set the rect of button "hxtPause" to 22,50,400,72
   create button "hxtEscape"
   set the style of button "hxtEscape" to "checkbox"
   set the label of button "hxtEscape" to "Allow do / value escape hatch"
   set the rect of button "hxtEscape" to 22,78,400,100
   create field "hxtTokenLabel"
   set the lockText of field "hxtTokenLabel" to true
   set the showBorder of field "hxtTokenLabel" to false
   set the rect of field "hxtTokenLabel" to 22,118,420,138
   put "Session token:" into field "hxtTokenLabel"
   create field "hxtToken"
   set the lockText of field "hxtToken" to true
   set the rect of field "hxtToken" to 22,140,420,162

   -- Log tab
   create field "hxtLog"
   set the lockText of field "hxtLog" to true
   set the dontWrap of field "hxtLog" to true
   set the vScrollbar of field "hxtLog" to true
   set the rect of field "hxtLog" to 16,46,424,322

   hxtShowTab 1
   hxtRefreshStatus
   set the defaultStack to tOldDefault
end hxtBuildUI

private command hxtShowTab pIndex
   if there is no button "hxtTabs" of me then exit hxtShowTab
   set the menuHistory of button "hxtTabs" of me to pIndex
   set the visible of graphic "hxtLight" of me to (pIndex is 1)
   set the visible of field "hxtStatusText" of me to (pIndex is 1)
   set the visible of button "hxtPause" of me to (pIndex is 2)
   set the visible of button "hxtEscape" of me to (pIndex is 2)
   set the visible of field "hxtTokenLabel" of me to (pIndex is 2)
   set the visible of field "hxtToken" of me to (pIndex is 2)
   set the visible of field "hxtLog" of me to (pIndex is 3)
   if pIndex is 3 then  -- sync the (lazily-updated) log when its tab becomes visible
      set the text of field "hxtLog" of me to sLog
      set the vScroll of field "hxtLog" of me to the formattedHeight of field "hxtLog" of me
   end if
end hxtShowTab

-- Update the traffic light, status text, token, and toggle states. No-op if the UI isn't built.
command hxtRefreshStatus
   if there is no field "hxtStatusText" of me then exit hxtRefreshStatus
   if sListenPort is not empty then
      set the backgroundColor of graphic "hxtLight" of me to "0,170,0"
      put "Listening on 127.0.0.1 port" && sListenPort into field "hxtStatusText" of me
   else
      set the backgroundColor of graphic "hxtLight" of me to "200,40,40"
      put "Stopped" into field "hxtStatusText" of me
   end if
   put sToken into field "hxtToken" of me
   set the hilite of button "hxtPause" of me to (sPaused is true)
   set the hilite of button "hxtEscape" of me to (sAllowDo is true)
end hxtRefreshStatus

-- Append a line to the Log tab's ring buffer. Only repaints the field if the Log tab is visible.
command hxtUILog pLine
   put (the long time && pLine) & return after sLog
   local tExtra
   put the number of lines of sLog - kLogMax into tExtra
   if tExtra > 0 then delete line 1 to tExtra of sLog
   if there is no field "hxtLog" of me then exit hxtUILog
   if the visible of field "hxtLog" of me then
      set the text of field "hxtLog" of me to sLog
      set the vScroll of field "hxtLog" of me to the formattedHeight of field "hxtLog" of me
   end if
end hxtUILog

-- UI wiring: tab clicks and the Settings checkboxes.
on menuPick pTab
   if the short name of the target is not "hxtTabs" then
      pass menuPick
   end if
   switch pTab
      case "Status"
         hxtShowTab 1
         break
      case "Settings"
         hxtShowTab 2
         break
      case "Log"
         hxtShowTab 3
         break
   end switch
end menuPick

on mouseUp
   switch the short name of the target
      case "hxtPause"
         put (the hilite of button "hxtPause" of me is true) into sPaused
         break
      case "hxtEscape"
         put (the hilite of button "hxtEscape" of me is true) into sAllowDo
         break
      default
         pass mouseUp
   end switch
end mouseUp


-- ============================================================ logging

-- Predictable path (home always resolves) so diagnostics survive even when paths/dispatch fail.
private function hxtLogPath
   return specialFolderPath("home") & "/hxt-mcp-bridge.log"
end hxtLogPath

private command hxtLog pMessage
   put (the seconds & "  " & pMessage & return) after url ("file:" & hxtLogPath())
end hxtLog


-- ============================================================ handshake

-- Per-user data folder, matching platformdirs.user_data_dir("HyperXTalk") on the Python side.
-- Resolved per-OS; on macOS we use an explicit home-based path (the folder tokens misbehaved here).
private function hxtHandshakeFolder
   local tBase
   switch the platform
      case "MacOS"
         -- Use the explicit home-based path: the "support"/"asup" tokens resolved to the SYSTEM
         -- /Library (unwritable) on this build. This matches platformdirs.user_data_dir on macOS.
         put specialFolderPath("home") & "/Library/Application Support" into tBase
         break
      case "Win32"
         put specialFolderPath("support") into tBase         -- %APPDATA%
         break
      default
         put specialFolderPath("home") & "/.local/share" into tBase  -- XDG data home
         break
   end switch
   if tBase is empty then return empty
   return tBase & "/HyperXTalk"
end hxtHandshakeFolder

private function hxtHandshakePath
   return hxtHandshakeFolder() & "/" & kHandshakeName
end hxtHandshakePath

-- Returns empty on success, or a description of why the write failed.
private function hxtWriteHandshake
   local tFolder
   put hxtHandshakeFolder() into tFolder
   if tFolder is empty then return "could not resolve the per-user data folder"

   -- Owner-only: the file carries the session token. umask 0077 => files 0600, folders 0700
   -- (POSIX; no-op on Windows). Restore afterwards so we don't affect the rest of the IDE.
   local tOldMask
   put the umask into tOldMask
   set the umask to 63
   if there is no folder tFolder then
      create folder tFolder
      if the result is not empty then
         set the umask to tOldMask
         return "create folder failed (" & the result & "):" && tFolder
      end if
   end if

   -- Controlled values (numeric port/pid/startedAt, hex token) -> build JSON directly.
   local tJson
   put "{" & hxtQ("port") & ":" & sListenPort & "," & \
      hxtQ("token") & ":" & hxtQ(sToken) & "," & \
      hxtQ("pid") & ":" & the processID & "," & \
      hxtQ("startedAt") & ":" & the seconds & "}" into tJson
   put tJson into url ("file:" & hxtHandshakePath())
   local tWriteErr
   put the result into tWriteErr
   set the umask to tOldMask
   if tWriteErr is not empty then return "file write failed (" & tWriteErr & "):" && hxtHandshakePath()
   if there is no file hxtHandshakePath() then return "write reported OK but file is absent:" && hxtHandshakePath()
   return empty
end hxtWriteHandshake

private command hxtDeleteHandshake
   local tPath
   put hxtHandshakePath() into tPath
   if there is a file tPath then delete file tPath
end hxtDeleteHandshake


-- ============================================================ socket reader (async)

on __HxtAccept pSocketID, pLocalPort
   put the milliseconds into sConn[pSocketID]["acceptedAt"]   -- track this connection (definite key)
   read from socket pSocketID until (crlf & crlf) with message "__HxtHeaders"
end __HxtAccept

on __HxtHeaders pSocketID, pData
   -- A socketClosed/timeout may have cleaned us up before this callback fired; don't resurrect state.
   if pSocketID is not among the lines of the openSockets then exit __HxtHeaders

   put word 1 of line 1 of pData into sConn[pSocketID]["method"]
   put word 2 of line 1 of pData into sConn[pSocketID]["path"]
   put hxtHeaderValue(pData, "x-hxt-token") into sConn[pSocketID]["token"]

   local tLen
   put hxtHeaderValue(pData, "content-length") into tLen
   if tLen is not a number then put 0 into tLen
   put tLen into sConn[pSocketID]["contentLength"]

   if tLen > kMaxBody then
      hxtRespond pSocketID, 413, hxtError(empty, "badarg", "request body exceeds 64 MB")
      exit __HxtHeaders
   end if

   if tLen > 0 then
      read from socket pSocketID for tLen with message "__HxtBody"
   else
      __HxtBody pSocketID, empty
   end if
end __HxtHeaders

on __HxtBody pSocketID, pData
   if pSocketID is not among the lines of the openSockets then exit __HxtBody
   hxtHandle pSocketID, textDecode(pData, "UTF-8")
end __HxtBody

private command hxtHandle pSocketID, pBody
   -- auth
   if sConn[pSocketID]["token"] is not sToken then
      hxtRespond pSocketID, 401, hxtError(empty, "unauthorized", "bad or missing token")
      exit hxtHandle
   end if
   -- routing
   if sConn[pSocketID]["method"] is not "POST" or sConn[pSocketID]["path"] is not "/rpc" then
      hxtRespond pSocketID, 404, hxtError(empty, "notfound", "use POST /rpc")
      exit hxtHandle
   end if

   local tReq, tId, tOperation, tParams
   try
      put JsonImport(pBody) into tReq
   catch tErr
      hxtRespond pSocketID, 400, hxtError(empty, "badarg", "invalid JSON request body")
      exit hxtHandle
   end try
   put tReq["id"] into tId
   put tReq["op"] into tOperation
   put tReq["params"] into tParams
   hxtUILog tOperation

   -- strict one-in-flight: only the async response-write window can overlap, so guard it.
   if sState is "busy" then
      hxtRespond pSocketID, 409, hxtError(tId, "busy", "another request is in flight")
      exit hxtHandle
   end if
   put "busy" into sState
   put pSocketID into sBusySocket

   -- Never let an op exception hang the bridge: always send a response (clears busy via __HxtWritten).
   local tBody
   try
      put hxtDispatch(tId, tOperation, tParams) into tBody
   catch tExc
      put hxtError(tId, "runtime", "op exception:" && tExc) into tBody
   end try
   hxtRespond pSocketID, 200, tBody
end hxtHandle


-- ============================================================ op dispatch

-- Builds native arrays for the result; hxtOk serialises via JsonExport (O(n), RFC-compliant).
private function hxtDispatch pId, pOperation, pParams
   local tResult
   switch pOperation
      case "ping"
         put the version into tResult["engine"]
         put the platform into tResult["platform"]
         put kBridgeVersion into tResult["bridge"]
         return hxtOk(pId, tResult)
      case "echo"
         -- byte-for-byte round-trip check (exercises the Content-Length reader + JsonExport).
         put pParams["data"] into tResult["data"]
         return hxtOk(pId, tResult)
      case "__busytest"
         -- test-only: hold the busy token across an event-loop yield so a 2nd request gets 409.
         local tMs
         put pParams["ms"] into tMs
         if tMs is not a number then put 0 into tMs
         wait tMs milliseconds with messages
         put tMs into tResult["waited"]
         return hxtOk(pId, tResult)
      case "stacks.list"
         return hxtOpStacksList(pId)
      case "tree.get"
         return hxtOpTreeGet(pId, pParams)
      case "object.getProps"
         return hxtOpGetProps(pId, pParams)
      case "object.getScript"
         return hxtOpGetScript(pId, pParams)
      case "bridge.safeToEdit"
         return hxtOpSafeToEdit(pId, pParams)
      case "bridge.setPaused"
         return hxtOpSetPaused(pId, pParams)
      case "control.create"
         return hxtOpControlCreate(pId, pParams)
      case "widget.create"
         return hxtOpWidgetCreate(pId, pParams)
      case "object.delete"
         return hxtOpDelete(pId, pParams)
      case "object.clone"
         return hxtOpClone(pId, pParams)
      case "object.setProps"
         return hxtOpSetProps(pId, pParams)
      case "object.setScript"
         return hxtOpSetScript(pId, pParams)
      case "stack.save"
         return hxtOpStackSave(pId, pParams)
      case "stack.saveAs"
         return hxtOpStackSaveAs(pId, pParams)
      default
         return hxtError(pId, "notfound", "unknown op:" && pOperation)
   end switch
end hxtDispatch


-- ============================================================ discovery + read (phase 2)

-- stacks.list -> the open USER stacks (IDE/system stacks + the bridge itself excluded).
private function hxtOpStacksList pId
   local tResult, tStacks, tName, n
   put 0 into n
   repeat for each line tName in the openStacks
      -- never list the bridge's own stack (dynamic, with the known name as a backstop)
      if tName is the short name of me or tName is "hxt-mcp-bridge" then next repeat
      if hxtIsIDEStack(tName) then next repeat
      add 1 to n
      put tName into tStacks[n]["name"]
      put hxtStackHandle(tName) into tStacks[n]["handle"]
      put the effective filename of stack tName into tStacks[n]["file"]
      put (the mainStack of stack tName is tName) into tStacks[n]["isMainStack"]
      put the number of cards of stack tName into tStacks[n]["cardCount"]
   end repeat
   put tStacks into tResult["stacks"]
   return hxtOk(pId, tResult)
end hxtOpStacksList

-- tree.get -> cards (with nested control tree) for a stack handle.
private function hxtOpTreeGet pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   if tComp["kind"] is not "stack" then return hxtError(pId, "badarg", "tree.get expects a stack handle")

   local tName, tCardIds, tCardId, tResult, tCards, n
   put tComp["stack"] into tName
   put the cardIDs of stack tName into tCardIds
   put 0 into n
   repeat for each line tCardId in tCardIds
      add 1 to n
      put tCardId into tCards[n]["id"]
      put the short name of card id tCardId of stack tName into tCards[n]["name"]
      put hxtObjHandle(tName, tCardId, "card", tCardId) into tCards[n]["handle"]
      put hxtCardControls(tName, tCardId) into tCards[n]["controls"]
   end repeat
   put the short name of stack tName into tResult["name"]
   put hxtStackHandle(tName) into tResult["handle"]
   put tCards into tResult["cards"]
   return hxtOk(pId, tResult)
end hxtOpTreeGet

-- object.getProps -> the (settable, JSON-safe) properties plus id/name/type/kind.
private function hxtOpGetProps pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")

   local tName, tCardId, tObjId, tProps
   put tComp["stack"] into tName
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   switch tComp["kind"]
      case "stack"
         put the properties of stack tName into tProps
         put the id of stack tName into tProps["id"]
         put the short name of stack tName into tProps["name"]
         put "stack" into tProps["type"]
         break
      case "card"
         put the properties of card id tCardId of stack tName into tProps
         put tCardId into tProps["id"]
         put the short name of card id tCardId of stack tName into tProps["name"]
         put "card" into tProps["type"]
         break
      default
         put the properties of control id tObjId of card id tCardId of stack tName into tProps
         put tObjId into tProps["id"]
         put the short name of control id tObjId of card id tCardId of stack tName into tProps["name"]
         put word 1 of the long id of control id tObjId of card id tCardId of stack tName into tProps["type"]
         if tProps["type"] is "widget" then
            put the kind of widget id tObjId of card id tCardId of stack tName into tProps["kind"]
         end if
   end switch
   local tResult
   put tProps into tResult["props"]
   return hxtOk(pId, tResult)
end hxtOpGetProps

-- object.getScript -> the script of any object.
private function hxtOpGetScript pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")

   local tName, tCardId, tObjId, tScript
   put tComp["stack"] into tName
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   switch tComp["kind"]
      case "stack"
         put the script of stack tName into tScript
         break
      case "card"
         put the script of card id tCardId of stack tName into tScript
         break
      default
         put the script of control id tObjId of card id tCardId of stack tName into tScript
   end switch
   local tResult
   put tScript into tResult["script"]
   return hxtOk(pId, tResult)
end hxtOpGetScript


-- ============================================================ safe-to-edit (phase 3)

-- bridge.safeToEdit -> {safe, reason}. The reason vocabulary:
--   paused | stack_cant_modify | cant_delete | open_in_editor
private function hxtOpSafeToEdit pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   local tOperation, tReason, tResult
   put pParams["operation"] into tOperation
   if tOperation is empty then put "edit" into tOperation
   put hxtSafeToEdit(tComp, tOperation) into tReason
   put (tReason is empty) into tResult["safe"]
   if tReason is not empty then put tReason into tResult["reason"]
   return hxtOk(pId, tResult)
end hxtOpSafeToEdit

-- bridge.setPaused -> set/clear the master pause switch.
private function hxtOpSetPaused pId, pParams
   if pParams["paused"] is true or pParams["paused"] is "true" then
      put true into sPaused
   else
      put false into sPaused
   end if
   local tResult
   put sPaused into tResult["paused"]
   return hxtOk(pId, tResult)
end hxtOpSetPaused

-- Returns empty if it's safe to mutate the resolved object, else a reason string.
-- pOperation: "setScript" | "setProps" | "delete" | "create" | "edit". Used by phase-4 mutations.
-- NOTE: active drag and group-edit mode are NOT queryable from xTalk; the manual pause switch is the
-- coarse guard for "user is interacting". Executing-script and compiled-library are handled at the
-- mutation site (try/catch + read-back verify) since neither is queryable up front.
private function hxtSafeToEdit pComp, pOperation
   if sPaused is true then return "paused"   -- `is true` so an uninitialized empty doesn't throw

   local tName, tCardId, tObjId, tKind
   put pComp["stack"] into tName
   put pComp["cardId"] into tCardId
   put pComp["objId"] into tObjId
   put pComp["kind"] into tKind

   -- the author marked the stack non-editable (typically a running stack)
   if the cantModify of stack tName then return "stack_cant_modify"

   -- a delete the engine would refuse anyway. `the cantDelete` throws EE_OBJECT_GETNOPROP on some
   -- control types (e.g. plain buttons), so guard the get and treat "unsupported" as deletable.
   if pOperation is "delete" and tKind is not "stack" then
      local tCantDelete
      put false into tCantDelete
      try
         if tKind is "card" then
            put the cantDelete of card id tCardId of stack tName into tCantDelete
         else
            put the cantDelete of control id tObjId of card id tCardId of stack tName into tCantDelete
         end if
      catch tIgnore
         put false into tCantDelete
      end try
      if tCantDelete is true then return "cant_delete"
   end if

   -- would clobber unsaved work if the object's script is open in the IDE script editor
   local tLongId, tEditing
   switch tKind
      case "stack"
         put the long id of stack tName into tLongId
         break
      case "card"
         put the long id of card id tCardId of stack tName into tLongId
         break
      default
         put the long id of control id tObjId of card id tCardId of stack tName into tLongId
   end switch
   try
      put revIDEScriptEditingObjects() into tEditing
   catch tIgnore
      put empty into tEditing   -- IDE library absent (headless) -> can't check; treat as not-open
   end try
   if tLongId is among the lines of tEditing then return "open_in_editor"

   return empty
end hxtSafeToEdit


-- ============================================================ mutations (phase 4)

-- A chunk expression (as a string, for use only inside `do`) referencing a card or group parent.
-- Returns empty if the parent isn't a card/group or the stack name is unsafe to interpolate.
private function hxtParentChunk pComp
   local tName, tCardId
   put pComp["stack"] into tName
   put pComp["cardId"] into tCardId
   if tName contains quote then return empty
   switch pComp["kind"]
      case "card"
         return "card id" && tCardId && "of stack" && quote & tName & quote
      case "group"
         return "group id" && pComp["objId"] && "of card id" && tCardId && "of stack" && quote & tName & quote
      default
         return empty
   end switch
end hxtParentChunk

-- control.create {parentHandle, type, name?, props?} -> {id, type, handle}
private function hxtOpControlCreate pId, pParams
   local tPComp
   put hxtResolveComp(pParams["parentHandle"]) into tPComp
   if tPComp["error"] is not empty then return hxtError(pId, tPComp["error"], "could not resolve parent handle")
   local tReason
   put hxtSafeToEdit(tPComp, "create") into tReason
   if tReason is not empty then return hxtError(pId, "not_safe_to_edit", tReason)

   local tType
   put pParams["type"] into tType
   if tType is not among the items of kCreatableTypes then
      return hxtError(pId, "badarg", "type must be one of:" && kCreatableTypes)
   end if
   local tParent
   put hxtParentChunk(tPComp) into tParent
   if tParent is empty then return hxtError(pId, "badarg", "parent must be a card or group")

   -- dynamic type requires `do`; inputs are whitelisted type + numeric ids + validated stack name.
   -- Capture the new id INSIDE the do (it doesn't reliably propagate out via `it`).
   put empty into sLastCreatedId
   do ("create" && tType && "in" && tParent & return & "put the id of it into sLastCreatedId")
   if the result is not empty then return hxtError(pId, "runtime", "create failed:" && the result)
   if sLastCreatedId is empty then return hxtError(pId, "runtime", "create produced no object id")
   local tNewId, tStk, tCardId
   put sLastCreatedId into tNewId
   put tPComp["stack"] into tStk
   put tPComp["cardId"] into tCardId
   return hxtFinishCreate(pId, tStk, tCardId, tNewId, pParams)
end hxtOpControlCreate

-- widget.create {parentHandle, kind, name?, props?} -> {id, type, kind, handle}
private function hxtOpWidgetCreate pId, pParams
   local tPComp
   put hxtResolveComp(pParams["parentHandle"]) into tPComp
   if tPComp["error"] is not empty then return hxtError(pId, tPComp["error"], "could not resolve parent handle")
   local tReason
   put hxtSafeToEdit(tPComp, "create") into tReason
   if tReason is not empty then return hxtError(pId, "not_safe_to_edit", tReason)

   local tKind
   put pParams["kind"] into tKind
   if tKind is empty or tKind contains quote or tKind contains space or tKind contains return then
      return hxtError(pId, "badarg", "kind must be a widget module id (e.g. com.livecode.widget.list)")
   end if
   local tParent
   put hxtParentChunk(tPComp) into tParent
   if tParent is empty then return hxtError(pId, "badarg", "parent must be a card or group")

   put empty into sLastCreatedId
   do ("create widget as" && quote & tKind & quote && "in" && tParent & return & \
      "put the id of it into sLastCreatedId")
   if the result is not empty then return hxtError(pId, "runtime", "create widget failed:" && the result)
   if sLastCreatedId is empty then return hxtError(pId, "runtime", "create widget produced no object id")
   local tNewId, tStk, tCardId
   put sLastCreatedId into tNewId
   put tPComp["stack"] into tStk
   put tPComp["cardId"] into tCardId
   return hxtFinishCreate(pId, tStk, tCardId, tNewId, pParams)
end hxtOpWidgetCreate

-- Shared create tail: optional name + props, then mint the handle.
private function hxtFinishCreate pId, pStk, pCardId, pNewId, pParams
   -- Transactional: if name/props setting fails, delete the just-created object (don't leak it).
   try
      if pParams["name"] is not empty then
         set the name of control id pNewId of card id pCardId of stack pStk to pParams["name"]
      end if
      if pParams["props"] is an array then
         set the properties of control id pNewId of card id pCardId of stack pStk to pParams["props"]
      end if
   catch tInitErr
      try
         delete control id pNewId of card id pCardId of stack pStk
      catch tIgnore
      end try
      return hxtError(pId, "runtime", "failed to initialize new object:" && tInitErr)
   end try
   local tType, tResult
   put word 1 of the long id of control id pNewId of card id pCardId of stack pStk into tType
   put pNewId into tResult["id"]
   put tType into tResult["type"]
   if tType is "widget" then
      put the kind of widget id pNewId of card id pCardId of stack pStk into tResult["kind"]
   end if
   put hxtObjHandle(pStk, pCardId, tType, pNewId) into tResult["handle"]
   return hxtOk(pId, tResult)
end hxtFinishCreate

-- object.delete {handle} -> {deleted:true}
private function hxtOpDelete pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   local tReason
   put hxtSafeToEdit(tComp, "delete") into tReason
   if tReason is not empty then return hxtError(pId, "not_safe_to_edit", tReason)
   local tName, tCardId, tObjId
   put tComp["stack"] into tName
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   switch tComp["kind"]
      case "stack"
         return hxtError(pId, "badarg", "refusing to delete a stack via object.delete")
      case "card"
         delete card id tCardId of stack tName
         break
      default
         delete control id tObjId of card id tCardId of stack tName
   end switch
   local tResult
   put true into tResult["deleted"]
   return hxtOk(pId, tResult)
end hxtOpDelete

-- object.clone {handle, name?} -> {id, type, handle}
private function hxtOpClone pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   if tComp["kind"] is "stack" or tComp["kind"] is "card" then
      return hxtError(pId, "badarg", "object.clone supports controls/widgets only")
   end if
   local tReason
   put hxtSafeToEdit(tComp, "create") into tReason
   if tReason is not empty then return hxtError(pId, "not_safe_to_edit", tReason)
   local tName, tCardId, tObjId
   put tComp["stack"] into tName
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   clone control id tObjId of card id tCardId of stack tName
   if the result is not empty then return hxtError(pId, "runtime", "clone failed:" && the result)
   local tNewId
   put the id of it into tNewId
   return hxtFinishCreate(pId, tName, tCardId, tNewId, pParams)
end hxtOpClone

-- object.setProps {handle, props} -> {ok}. Partial update via `set the properties`.
private function hxtOpSetProps pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   if pParams["props"] is not an array then return hxtError(pId, "badarg", "props must be an object")
   local tReason
   put hxtSafeToEdit(tComp, "setProps") into tReason
   if tReason is not empty then return hxtError(pId, "not_safe_to_edit", tReason)
   local tName, tCardId, tObjId
   put tComp["stack"] into tName
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   switch tComp["kind"]
      case "stack"
         set the properties of stack tName to pParams["props"]
         break
      case "card"
         set the properties of card id tCardId of stack tName to pParams["props"]
         break
      default
         set the properties of control id tObjId of card id tCardId of stack tName to pParams["props"]
   end switch
   local tResult
   put true into tResult["ok"]
   return hxtOk(pId, tResult)
end hxtOpSetProps

-- object.setScript {handle, script} -> {ok} | compile error | compiled-library no-op.
-- Transactional: cache old, set new, surface compile errors (restoring old), and detect the
-- silent no-op of compiled libraries by reading back.
private function hxtOpSetScript pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   local tReason
   put hxtSafeToEdit(tComp, "setScript") into tReason
   if tReason is not empty then return hxtError(pId, "not_safe_to_edit", tReason)

   local tNew, tExpected
   put pParams["script"] into tNew
   put tNew into tExpected
   if tExpected is not empty and the last char of tExpected is not return then
      put return after tExpected   -- engine appends a trailing newline on set
   end if

   local tName, tCardId, tObjId, tOld, tErr, tBack
   put tComp["stack"] into tName
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   switch tComp["kind"]
      case "stack"
         put the script of stack tName into tOld
         set the script of stack tName to tNew
         put the result into tErr
         put the script of stack tName into tBack
         if tErr is not empty then set the script of stack tName to tOld
         break
      case "card"
         put the script of card id tCardId of stack tName into tOld
         set the script of card id tCardId of stack tName to tNew
         put the result into tErr
         put the script of card id tCardId of stack tName into tBack
         if tErr is not empty then set the script of card id tCardId of stack tName to tOld
         break
      default
         put the script of control id tObjId of card id tCardId of stack tName into tOld
         set the script of control id tObjId of card id tCardId of stack tName to tNew
         put the result into tErr
         put the script of control id tObjId of card id tCardId of stack tName into tBack
         if tErr is not empty then set the script of control id tObjId of card id tCardId of stack tName to tOld
   end switch

   if tErr is not empty then return hxtError(pId, "compile", tErr)
   if tBack is not tExpected then
      return hxtError(pId, "runtime", "set script had no effect (compiled library or locked object)")
   end if
   local tResult
   put true into tResult["ok"]
   return hxtOk(pId, tResult)
end hxtOpSetScript

-- stack.save {handle} -> {file}
private function hxtOpStackSave pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   if tComp["kind"] is not "stack" then return hxtError(pId, "badarg", "stack.save expects a stack handle")
   local tName
   put tComp["stack"] into tName
   if the effective filename of stack tName is empty then
      return hxtError(pId, "badarg", "stack has never been saved; use stack.saveAs")
   end if
   save stack tName
   if the result is not empty then return hxtError(pId, "runtime", "save failed:" && the result)
   local tResult
   put the effective filename of stack tName into tResult["file"]
   return hxtOk(pId, tResult)
end hxtOpStackSave

-- stack.saveAs {handle, fileName} -> {file}. The bridge resolves the path (Documents folder).
private function hxtOpStackSaveAs pId, pParams
   local tComp
   put hxtResolveComp(pParams["handle"]) into tComp
   if tComp["error"] is not empty then return hxtError(pId, tComp["error"], "could not resolve handle")
   if tComp["kind"] is not "stack" then return hxtError(pId, "badarg", "stack.saveAs expects a stack handle")
   local tFile
   put pParams["fileName"] into tFile
   if tFile is empty or tFile contains "/" or tFile contains "\" or tFile contains ":" \
      or tFile contains ".." then
      return hxtError(pId, "badarg", "fileName must be a bare file name (no path separators)")
   end if
   if not (tFile ends with ".livecode") and not (tFile ends with ".hyperxtalk") then
      put tFile & ".livecode" into tFile
   end if
   local tPath, tName
   put tComp["stack"] into tName
   put specialFolderPath("documents") & "/" & tFile into tPath
   save stack tName as tPath
   if the result is not empty then return hxtError(pId, "runtime", "saveAs failed:" && the result)
   local tResult
   put tPath into tResult["file"]
   return hxtOk(pId, tResult)
end hxtOpStackSaveAs


-- ---- control-tree enumeration (number of controls is FLAT, so filter top-level by owner) ----

private function hxtCardControls pStack, pCardId
   local tCtrls, n, i, tCount, tCid, tOwnerType
   put 0 into n
   put the number of controls of card id pCardId of stack pStack into tCount
   repeat with i = 1 to tCount
      put the id of control i of card id pCardId of stack pStack into tCid
      put word 1 of the long owner of control id tCid of card id pCardId of stack pStack into tOwnerType
      -- top-level = owned by the card, OR by the stack (shared/background groups placed on the card).
      -- Controls owned by a group are emitted via that group's children, so skip them here.
      if tOwnerType is not "card" and tOwnerType is not "stack" then next repeat
      add 1 to n
      put hxtControlEntry(pStack, pCardId, tCid) into tCtrls[n]
   end repeat
   return tCtrls
end hxtCardControls

private function hxtGroupChildren pStack, pCardId, pGroupId
   local tKids, n, tChildIds, tChildId
   put 0 into n
   put the childControlIDs of group id pGroupId of card id pCardId of stack pStack into tChildIds
   repeat for each line tChildId in tChildIds
      add 1 to n
      put hxtControlEntry(pStack, pCardId, tChildId) into tKids[n]
   end repeat
   return tKids
end hxtGroupChildren

private function hxtControlEntry pStack, pCardId, pCid
   local tEntry, tType
   put word 1 of the long id of control id pCid of card id pCardId of stack pStack into tType
   put pCid into tEntry["id"]
   put the short name of control id pCid of card id pCardId of stack pStack into tEntry["name"]
   put tType into tEntry["type"]
   put hxtObjHandle(pStack, pCardId, tType, pCid) into tEntry["handle"]
   if tType is "widget" then
      put the kind of widget id pCid of card id pCardId of stack pStack into tEntry["kind"]
   end if
   if tType is "group" then
      put hxtGroupChildren(pStack, pCardId, pCid) into tEntry["children"]
   end if
   return tEntry
end hxtControlEntry


-- ---- handles + resolution ----

private function hxtIsIDEStack pName
   local tIsIDE
   try
      put revStackNameIsIDEStack(pName) into tIsIDE
      return tIsIDE
   catch tIgnore
      -- headless / IDE library absent: conservative fallback
      if pName begins with "rev" then return true
      if pName begins with "com.livecode." then return true
      if pName is among the items of "message box,answer dialog,ask dialog,home" then return true
      return false
   end try
end hxtIsIDEStack

private function hxtStackHandle pName
   local tH
   put 1 into tH["v"]
   put pName into tH["stack"]["shortName"]
   put the mainStack of stack pName into tH["stack"]["mainStackName"]
   put the effective filename of stack pName into tH["stack"]["fileName"]
   put the id of stack pName into tH["stack"]["stackId"]
   put 0 into tH["cardId"]
   put "stack" into tH["objType"]
   put the id of stack pName into tH["objId"]
   return hxtEncodeHandle(tH)
end hxtStackHandle

private function hxtObjHandle pStackName, pCardId, pType, pObjId
   local tH
   put 1 into tH["v"]
   put pStackName into tH["stack"]["shortName"]
   put the mainStack of stack pStackName into tH["stack"]["mainStackName"]
   put the effective filename of stack pStackName into tH["stack"]["fileName"]
   put the id of stack pStackName into tH["stack"]["stackId"]
   put pCardId into tH["cardId"]
   put pType into tH["objType"]
   put pObjId into tH["objId"]
   return hxtEncodeHandle(tH)
end hxtObjHandle

private function hxtEncodeHandle pArray
   local tEnc
   put base64Encode(textEncode(JsonExport(pArray), "UTF-8")) into tEnc
   replace return with empty in tEnc
   replace linefeed with empty in tEnc
   return tEnc
end hxtEncodeHandle

private function hxtDecodeHandle pHandle
   local tArr
   try
      put JsonImport(textDecode(base64Decode(pHandle), "UTF-8")) into tArr
      return tArr
   catch tIgnore
      return empty
   end try
end hxtDecodeHandle

-- Resolve a handle fail-closed against the open-stack set. Returns a comp array;
-- comp["error"] is set ("badarg" | "stale_handle") if it can't be resolved.
private function hxtResolveComp pHandle
   local tH, tComp
   put hxtDecodeHandle(pHandle) into tH
   if tH is empty or tH["stack"] is not an array then
      put "badarg" into tComp["error"]
      return tComp
   end if
   -- Injection guard: cardId/objId are interpolated into chunk expressions (and a `do` on create),
   -- so a crafted handle must not smuggle xTalk through them. They must be plain integers.
   if tH["cardId"] is not an integer or tH["objId"] is not an integer then
      put "badarg" into tComp["error"]
      return tComp
   end if

   local tName
   put hxtFindOpenStack(tH["stack"]) into tName
   if tName is empty then
      put "stale_handle" into tComp["error"]
      return tComp
   end if
   put tName into tComp["stack"]
   put tH["cardId"] into tComp["cardId"]
   put tH["objId"] into tComp["objId"]
   put tH["objType"] into tComp["kind"]

   local tCardId, tObjId, tExists
   put tComp["cardId"] into tCardId
   put tComp["objId"] into tObjId
   switch tComp["kind"]
      case "stack"
         put (there is a stack tName) into tExists
         break
      case "card"
         put (there is a card id tCardId of stack tName) into tExists
         break
      default
         -- guard the parent card too: the card may have been deleted since the handle was minted
         if there is a card id tCardId of stack tName then
            put (there is a control id tObjId of card id tCardId of stack tName) into tExists
         else
            put false into tExists
         end if
   end switch
   if not tExists then put "stale_handle" into tComp["error"]
   return tComp
end hxtResolveComp

-- Fail-closed: only ever returns the name of an already-open stack whose id matches.
-- Never references stack "name" speculatively (that can side-effect-load by filename).
private function hxtFindOpenStack pStackKey
   local tName
   repeat for each line tName in the openStacks
      if tName is pStackKey["shortName"] then
         if (the id of stack tName) is pStackKey["stackId"] then return tName
      end if
   end repeat
   return empty
end hxtFindOpenStack


-- ============================================================ HTTP response

private command hxtRespond pSocketID, pStatus, pBodyJson
   local tBytes, tHeader
   put textEncode(pBodyJson, "UTF-8") into tBytes
   put "HTTP/1.1" && pStatus && hxtStatusText(pStatus) & crlf & \
      "Content-Type: application/json" & crlf & \
      "Content-Length:" && (the number of bytes of tBytes) & crlf & \
      "Connection: close" & crlf & crlf into tHeader
   write (textEncode(tHeader, "UTF-8") & tBytes) to socket pSocketID with message "__HxtWritten"
end hxtRespond

on __HxtWritten pSocketID
   hxtCleanupSocket pSocketID
end __HxtWritten

on socketError pSocketID, pError
   hxtLog "socketError id=" & pSocketID & " listener=" & sListenPort & " err=" & pError
   hxtCleanupSocket pSocketID
end socketError

on socketClosed pSocketID
   hxtLog "socketClosed id=" & pSocketID & " listener=" & sListenPort
   hxtCleanupSocket pSocketID
end socketClosed

on socketTimeout pSocketID
   hxtLog "socketTimeout id=" & pSocketID & " listener=" & sListenPort
   hxtCleanupSocket pSocketID
end socketTimeout

private command hxtCleanupSocket pSocketID
   -- Only ever tear down a TRACKED CONNECTION socket (one in sConn). The listening socket is never
   -- in sConn, so this is robust regardless of how socketTimeout/socketError identify the listener.
   -- (socketTimeout fires for the idle listener every interval — it's always "pending".)
   if pSocketID is sListenPort then exit hxtCleanupSocket
   if pSocketID is not among the lines of the keys of sConn then exit hxtCleanupSocket
   if pSocketID is sBusySocket then
      put "idle" into sState
      put empty into sBusySocket
   end if
   if pSocketID is among the lines of the openSockets then close socket pSocketID
   delete variable sConn[pSocketID]
end hxtCleanupSocket


-- ============================================================ helpers

private function hxtMakeToken
   local tT
   repeat 40 times
      put char (random(16)) of "0123456789abcdef" after tT
   end repeat
   return tT
end hxtMakeToken

private function hxtHeaderValue pData, pName
   local tLine, tColon, tKey, tVal
   repeat for each line tLine in pData
      put offset(":", tLine) into tColon
      if tColon = 0 then next repeat
      put toLower(char 1 to (tColon - 1) of tLine) into tKey
      if tKey is toLower(pName) then
         put char (tColon + 1) to -1 of tLine into tVal
         replace numToChar(13) with empty in tVal
         if tVal is empty then return empty
         return word 1 to -1 of tVal
      end if
   end repeat
   return empty
end hxtHeaderValue

private function hxtQ pStr
   return quote & pStr & quote
end hxtQ

private function hxtOk pId, pResult
   local tResp
   put pId into tResp["id"]
   put true into tResp["ok"]
   put pResult into tResp["result"]
   return JsonExport(tResp)
end hxtOk

private function hxtError pId, pKind, pMessage
   local tResp
   put pId into tResp["id"]
   put false into tResp["ok"]
   put pKind into tResp["error"]["kind"]
   put pMessage into tResp["error"]["message"]
   return JsonExport(tResp)
end hxtError

private function hxtStatusText pStatus
   switch pStatus
      case 200
         return "OK"
      case 400
         return "Bad Request"
      case 401
         return "Unauthorized"
      case 404
         return "Not Found"
      case 409
         return "Conflict"
      case 413
         return "Payload Too Large"
      default
         return "Internal Server Error"
   end switch
end hxtStatusText
