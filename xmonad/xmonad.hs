-- vdesk xmonad config, compiled into the image as xmonad-x86_64-linux (no GHC at runtime).
-- Alt is the mod key: the Windows side keeps the Win key for itself.
import XMonad
import XMonad.Hooks.EwmhDesktops (ewmh, ewmhFullscreen)
import XMonad.Hooks.ManageDocks (avoidStruts, docks)
import XMonad.Hooks.ManageHelpers (doCenterFloat, isDialog)
import XMonad.Layout.NoBorders (smartBorders)
import XMonad.Util.EZConfig (additionalKeysP)
import XMonad.Util.SpawnOnce (spawnOnce)

main :: IO ()
main = xmonad . ewmhFullscreen . ewmh . docks $ vdeskConfig

vdeskConfig = def
    { terminal           = "xterm"
    , modMask            = mod1Mask
    , borderWidth        = 2
    , normalBorderColor  = "#2b2b2b"
    , focusedBorderColor = "#8aa0c0"
    , layoutHook         = avoidStruts . smartBorders $ layoutHook def
    , manageHook         = vdeskManage <+> manageHook def
    , startupHook        = do
        spawn "vdesk-panel"
        spawnOnce "f=$HOME/.config/vdesk/autostart; [ -f \"$f\" ] && sh \"$f\""
    }
    `additionalKeysP`
    [ ("M-p", spawn "vdesk-launcher")
    -- The default M-q recompiles with GHC, which the image does not have
    , ("M-q", restart "xmonad-x86_64-linux" True)
    -- Alt+Space may be taken by Windows for the viewer's system menu
    , ("M-\\", sendMessage NextLayout)
    ]

vdeskManage :: ManageHook
vdeskManage = composeAll
    [ isDialog --> doCenterFloat
    , className =? "Xmessage" --> doCenterFloat
    ]
