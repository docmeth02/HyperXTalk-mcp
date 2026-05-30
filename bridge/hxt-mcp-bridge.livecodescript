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

local sListenPort        -- the listening (server) socket id == its port; empty when stopped
local sToken             -- per-session shared secret
local sState             -- "idle" | "busy" (strict one-in-flight)
local sBusySocket        -- the connection socket currently holding the busy token
local sConn              -- per-connection scratch: sConn[socket]["token"|"method"|"path"|"contentLength"]


-- ============================================================ lifecycle

on openStack
   hxtBridgeStart
end openStack

on closeStack
   hxtBridgeStop
end closeStack

-- Public so it can be started manually from the message box during development.
-- Returns empty on success, or an error string. Also logs to ~/hxt-mcp-bridge.log either way,
-- because `dispatch` (used by some callers) discards the return value.
command hxtBridgeStart
   if sListenPort is not empty then return "already running on port" && sListenPort
   try
      hxtLog "start: platform=" & the platform & ", folder=" & hxtHandshakeFolder()
      put "idle" into sState
      put empty into sBusySocket
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
      return empty
   catch tException
      hxtLog "EXCEPTION in hxtBridgeStart:" && tException
      return "exception:" && tException
   end try
end hxtBridgeStart

command hxtBridgeStop
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
end hxtBridgeStop

function hxtBridgeStatus
   return "port:" && sListenPort & comma & "state:" && sState
end hxtBridgeStatus


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
   put empty into sConn[pSocketID]
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

   -- strict one-in-flight: only the async response-write window can overlap, so guard it.
   if sState is "busy" then
      hxtRespond pSocketID, 409, hxtError(tId, "busy", "another request is in flight")
      exit hxtHandle
   end if
   put "busy" into sState
   put pSocketID into sBusySocket

   hxtRespond pSocketID, 200, hxtDispatch(tId, tOperation, tParams)
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
      default
         return hxtError(pId, "notfound", "unknown op:" && pOperation)
   end switch
end hxtDispatch


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
   hxtCleanupSocket pSocketID
end socketError

on socketClosed pSocketID
   hxtCleanupSocket pSocketID
end socketClosed

on socketTimeout pSocketID
   hxtCleanupSocket pSocketID
end socketTimeout

private command hxtCleanupSocket pSocketID
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
