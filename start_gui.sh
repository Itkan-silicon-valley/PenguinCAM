#!/bin/bash
# FRC CAM Post-Processor GUI Launcher
# For Mac and Linux

echo "=========================================="
echo "FRC CAM Post-Processor GUI"
echo "=========================================="
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is not installed"
    echo "Please install Python 3.8 or higher"
    exit 1
fi

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "❌ Error: pip is not installed"
    echo "Please install pip"
    exit 1
fi

# Check if dependencies are installed
echo "📦 Checking dependencies..."
python3 -c "import flask" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "⚙️  Installing dependencies..."
    pip3 install -r requirements_gui.txt
    if [ $? -ne 0 ]; then
        echo "❌ Failed to install dependencies"
        exit 1
    fi
fi

# Check if post-processor exists
if [ ! -f "frc_cam_postprocessor.py" ]; then
    echo "❌ Error: frc_cam_postprocessor.py not found"
    echo "Please make sure it's in the same directory as this script"
    exit 1
fi

# Check if templates directory exists
if [ ! -d "templates" ]; then
    echo "❌ Error: templates/ directory not found"
    echo ""
    echo "You need this structure:"
    echo "  your-directory/"
    echo "  ├── frc_cam_gui_app.py"
    echo "  ├── frc_cam_postprocessor.py"
    echo "  └── templates/"
    echo "      └── index.html"
    echo ""
    echo "Create templates directory and put index.html inside it:"
    echo "  mkdir templates"
    echo "  mv index.html templates/  (if index.html is in current directory)"
    exit 1
fi

# Check if index.html exists in templates
if [ ! -f "templates/index.html" ]; then
    echo "❌ Error: templates/index.html not found"
    echo ""
    echo "index.html must be inside the templates/ directory"
    echo ""
    if [ -f "index.html" ]; then
        echo "Found index.html in current directory. Moving it..."
        mv index.html templates/
        echo "✓ Fixed! index.html moved to templates/"
    else
        echo "index.html not found. Please download it from /mnt/user-data/outputs/"
        exit 1
    fi
fi

echo "✓ All dependencies ready"
echo ""
echo "🚀 Starting server..."
echo "📂 The browser will open automatically"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""
echo "=========================================="
echo ""

export PENGUINCAM_LOCAL_MODE=1
export PENGUINCAM_CONFIG="${PENGUINCAM_CONFIG:-yaml/PenguinCAM-config.yaml}"

# Function to cleanup on exit
cleanup() {
    echo ""
    echo "🛑 Stopping server..."
    
    # Kill the Flask server process and all its children
    if [ ! -z "$SERVER_PID" ]; then
        # Kill the process group (Flask spawns child processes in debug mode)
        pkill -P $SERVER_PID 2>/dev/null
        kill $SERVER_PID 2>/dev/null
        
        # Wait a moment for graceful shutdown
        sleep 1
        
        # Force kill if still running
        kill -9 $SERVER_PID 2>/dev/null
        pkill -9 -f "frc_cam_gui_app.py" 2>/dev/null
    fi
    
    echo "✓ Server stopped"
    echo "✓ Port 6238 released"
    exit 0
}

# Set up trap to catch Ctrl+C and other termination signals
trap cleanup SIGINT SIGTERM EXIT

# Start the server in background
python3 frc_cam_gui_app.py &
SERVER_PID=$!

# Wait a moment for server to start
sleep 2

# Open browser
if command -v open &> /dev/null; then
    # macOS
    open http://localhost:6238
elif command -v xdg-open &> /dev/null; then
    # Linux
    xdg-open http://localhost:6238
fi

echo "Server PID: $SERVER_PID"
echo ""

# Wait for server process (will be interrupted by Ctrl+C)
wait $SERVER_PID
