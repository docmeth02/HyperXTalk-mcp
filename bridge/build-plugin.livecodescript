script "hxt-mcp-build-plugin"

-- Builds and installs the bridge as a binary plugin stack from the script-only source.
-- A script-only stack cannot be opened as a plugin window (it has no cards), so we copy its script
-- body into a real (binary) stack saved in the user Plugins folder. The .livecodescript stays the
-- source of truth.
--
-- Usage in the HyperXTalk message box (replace <repo> with the checkout path):
--   start using stack "<repo>/bridge/build-plugin.livecodescript"
--   hxtBuildPlugin "<repo>/bridge/hxt-mcp-bridge.livecodescript"
-- It installs the plugin AND starts it immediately (no menu reopen needed during development).
-- For normal use afterwards: Development > Plugins > hxt-mcp-bridge.

command hxtBuildPlugin pScriptPath
   if there is no file pScriptPath then return "no such file:" && pScriptPath

   local tPlugins, tDest
   put revEnvironmentUserPluginsPath() into tPlugins
   if tPlugins is empty then return "could not resolve user plugins folder"
   if there is no folder tPlugins then create folder tPlugins
   put tPlugins & "/hxt-mcp-bridge.livecode" into tDest

   -- Clear any existing in-memory bridge stack (running, or broken from a prior attempt).
   hxtKillBridgeStack

   -- Read the source and strip the `script "..."` header line (that line is the script-only-stack
   -- file header, NOT part of the executable script body).
   local tScript
   put url ("file:" & pScriptPath) into tScript
   if word 1 of line 1 of tScript is "script" then delete line 1 of tScript

   create invisible stack "hxt-mcp-bridge"
   set the script of stack "hxt-mcp-bridge" to tScript
   if the result is not empty then
      return "COMPILE ERROR in bridge script:" && the result
   end if
   -- build the control-panel UI, then mark it a palette plugin
   local tUIErr
   try
      send "hxtBuildUI" to stack "hxt-mcp-bridge"
      put the result into tUIErr
   catch tErr
      put "exception:" && tErr into tUIErr
   end try
   if tUIErr is not empty then return "UI build error:" && tUIErr
   set the cREVLoadInfo["mode"] of stack "hxt-mcp-bridge" to "palette"
   save stack "hxt-mcp-bridge" as tDest

   -- show it as a palette; opening fires openStack -> hxtBridgeStart -> hxtRefreshStatus
   palette stack "hxt-mcp-bridge"
   return "installed to" && tDest & "; bridge started as a palette"
end hxtBuildPlugin

private command hxtKillBridgeStack
   if there is a stack "hxt-mcp-bridge" then
      try
         dispatch "hxtBridgeStop" to stack "hxt-mcp-bridge"
      catch tIgnore
      end try
      -- An OPEN palette with destroyStack=false won't fully delete; force it so the rebuild is clean.
      set the destroyStack of stack "hxt-mcp-bridge" to true
      set the dirty of stack "hxt-mcp-bridge" to false   -- avoid a save-confirmation prompt
      try
         close stack "hxt-mcp-bridge"
      catch tIgnore
      end try
      delete stack "hxt-mcp-bridge"
   end if
end hxtKillBridgeStack
