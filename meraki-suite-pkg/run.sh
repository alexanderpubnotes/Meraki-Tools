#!/usr/bin/env bash
# Launch the Meraki Suite GUI.  chmod +x run.sh  then  ./run.sh
cd "$(dirname "$0")"
python3 suite_gui.py || {
    echo
    echo "The app exited with an error. Read the message above."
    echo "Common fixes: install Python 3, then run: pip install meraki"
    read -p "Press Enter to close..."
}
