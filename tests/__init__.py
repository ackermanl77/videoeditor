import os

# Never ask GitHub for a newer release during the tests (update_check.py).
os.environ["VIDEO_EDITOR_UPDATE_FEED_URL"] = "off"
