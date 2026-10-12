' Run the given cmd script with a hidden window (scheduled task entry).
' Wait for the child so the task reflects service lifetime and
' RestartOnFailure can take effect when the service crashes.
Set sh = CreateObject("Wscript.Shell")
WScript.Quit sh.Run("""" & WScript.Arguments(0) & """", 0, True)
