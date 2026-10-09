from pathlib import Path
import bpy

root = Path(__file__).resolve().parents[1]

def capture():
    target = root / "tests/gui_saved_example.png"
    bpy.ops.screen.screenshot(filepath=str(target))
    print("SAVED_EXAMPLE_SCREENSHOT_OK", target)
    return None

bpy.app.timers.register(capture, first_interval=4.0)
