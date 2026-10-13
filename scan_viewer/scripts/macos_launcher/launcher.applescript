on open scanFiles
	set importScript to POSIX path of (path to resource "open_cloud.py")
	set logDirectory to POSIX path of (path to library folder from user domain) & "Logs/Scan Viewer"
	set blenderPath to "/Applications/Blender.app"
	set logFile to quoted form of (logDirectory & "/launcher.log")
	set launchCommand to "/bin/mkdir -p " & quoted form of logDirectory & "; /usr/bin/open -n -a " & quoted form of blenderPath & " --stdout " & logFile & " --stderr " & logFile & " --env PYTHONUNBUFFERED=1 --args --python " & quoted form of importScript & " --"
	repeat with scanFile in scanFiles
		set launchCommand to launchCommand & " " & quoted form of (POSIX path of scanFile)
	end repeat
	do shell script launchCommand
end open

on run
	set scanFiles to choose file with prompt "Choose a point cloud to open in Blender." with multiple selections allowed
	open scanFiles
end run
