"""
Smart Fridge Camera Server for Raspberry Pi
Production-ready version with environment variable support
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
import traceback
import threading
import certifi
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import pymongo
from bson.objectid import ObjectId
from picamera2 import Picamera2
from dotenv import load_dotenv

# Load environment variables
load_dotenv('.env.rpi')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('camera_app.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Flask App Configuration
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes
app.config['UPLOAD_FOLDER'] = "captured_images"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# MongoDB Atlas Configuration from environment
MONGO_URI = os.getenv('MONGO_URI')
IMAGE_RETENTION_DAYS = int(os.getenv('IMAGE_RETENTION_DAYS', '5'))
SERVER_PORT = int(os.getenv('SERVER_PORT', '5000'))

def get_mongo_connection():
    """Create and return a MongoDB connection."""
    try:
        client = pymongo.MongoClient(
            MONGO_URI,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=30000
        )
        client.admin.command('ping')
        logger.info("MongoDB connection successful")
        return client["SmartFridge"]
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        raise

def capture_image(output_dir='captured_images', max_retries=3):
    """
    Capture an image with simplified error handling and retry mechanism
    
    Args:
        output_dir (str): Directory to save captured images
        max_retries (int): Number of times to retry camera initialization
    
    Returns:
        tuple: (filepath, timestamp)
    """
    os.makedirs(output_dir, exist_ok=True)

    for attempt in range(max_retries):
        picam2 = None
        try:
            logger.info(f"Camera initialization attempt {attempt + 1} of {max_retries}")
            
            picam2 = Picamera2()
            logger.info("Camera object created")
            
            config = picam2.create_still_configuration()
            picam2.configure(config)
            logger.info("Camera configured")
            
            picam2.start()
            logger.info("Camera started")
            
            time.sleep(2)

            timestamp = datetime.now()
            filename = f"image_{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = os.path.join(output_dir, filename)

            logger.info(f"Capturing image to {filepath}")
            picam2.capture_file(filepath)
            
            if not os.path.exists(filepath):
                raise RuntimeError("Image file was not created")

            file_size = os.path.getsize(filepath)
            if file_size == 0:
                raise RuntimeError("Captured image is empty")

            logger.info(f"Image captured successfully ({file_size} bytes)")
            
            picam2.stop()
            picam2.close()
            
            return filepath, timestamp

        except Exception as e:
            logger.error(f"Camera capture attempt {attempt + 1} failed: {e}")
            logger.error(traceback.format_exc())
            
            if picam2:
                try:
                    picam2.stop()
                    picam2.close()
                    logger.info("Camera resources released")
                except Exception as cleanup_error:
                    logger.error(f"Error cleaning up camera resources: {cleanup_error}")
            
            time.sleep(3)

    raise RuntimeError("Failed to capture image after multiple attempts")
    
def cleanup_old_images(days=None):
    """
    Delete images older than specified days from storage and MongoDB
    
    Args:
        days (int): Number of days to retain images (defaults to IMAGE_RETENTION_DAYS)
    """
    if days is None:
        days = IMAGE_RETENTION_DAYS
        
    try:
        db = get_mongo_connection()
        images_collection = db["images"]

        cutoff_date = datetime.now() - timedelta(days=days)
        old_images = list(images_collection.find({"timestamp": {"$lt": cutoff_date}}))

        for img in old_images:
            try:
                if os.path.exists(img["path"]):
                    os.remove(img["path"])
                    logger.info(f"Deleted old image: {img['path']}")
            except Exception as e:
                logger.error(f"Error deleting file {img['path']}: {e}")
            
            images_collection.delete_one({"_id": img["_id"]})
        
        logger.info(f"Cleanup completed. Removed {len(old_images)} images older than {cutoff_date}")

    except Exception as e:
        logger.error(f"Image cleanup failed: {e}")

@app.route('/test', methods=['GET'])
def test_connection():
    """Endpoint to test if the server is running."""
    return jsonify({
        "status": "success",
        "message": "Smart Fridge Camera Server is online!",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0-rpi"
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Comprehensive health check endpoint."""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "components": {}
    }
    
    # Check MongoDB
    try:
        db = get_mongo_connection()
        db.command('ping')
        health_status["components"]["database"] = "ok"
    except Exception as e:
        health_status["components"]["database"] = f"error: {str(e)}"
        health_status["status"] = "degraded"
    
    # Check camera (basic check)
    try:
        if os.path.exists('/dev/video0'):
            health_status["components"]["camera"] = "ok"
        else:
            health_status["components"]["camera"] = "not_found"
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["components"]["camera"] = f"error: {str(e)}"
        health_status["status"] = "degraded"
    
    # Check disk space
    try:
        stat = os.statvfs(app.config['UPLOAD_FOLDER'])
        free_space_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        health_status["components"]["disk_space_gb"] = round(free_space_gb, 2)
        if free_space_gb < 1:
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["components"]["disk_space"] = f"error: {str(e)}"
    
    return jsonify(health_status)

@app.route('/capture', methods=['GET'])
def capture_image_endpoint():
    """Flask endpoint to capture and store an image."""
    try:
        filepath, timestamp = capture_image()
        logger.info(f"Image captured at {filepath}")

        try:
            db = get_mongo_connection()
            images_collection = db["images"]

            file_size = os.path.getsize(filepath)
            image_metadata = {
                "filename": os.path.basename(filepath),
                "path": filepath,
                "timestamp": timestamp,
                "size": file_size,
                "resolution": "1920x1080",
                "device": "raspberry_pi"
            }

            result = images_collection.insert_one(image_metadata)
            logger.info(f"Image metadata stored with ID: {result.inserted_id}")

            # Cleanup old images in background
            threading.Thread(target=cleanup_old_images, daemon=True).start()

            return jsonify({
                "status": "success",
                "message": "Image captured successfully!",
                "image_path": filepath,
                "image_id": str(result.inserted_id),
                "timestamp": timestamp.isoformat()
            })
        except Exception as db_error:
            logger.error(f"Database operation failed: {db_error}")
            return jsonify({
                "status": "partial_success",
                "message": f"Image captured but database storage failed: {str(db_error)}",
                "image_path": filepath,
                "timestamp": timestamp.isoformat()
            }), 207

    except Exception as e:
        logger.error(f"Capture endpoint failed: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/latest_image', methods=['GET'])
def get_latest_image():
    """Retrieve the most recently captured image."""
    try:
        db = get_mongo_connection()
        images_collection = db["images"]
        
        latest_image = images_collection.find_one(
            sort=[("timestamp", pymongo.DESCENDING)]
        )
        
        if not latest_image:
            return jsonify({"status": "error", "message": "No images found"}), 404
        
        if not os.path.exists(latest_image["path"]):
            return jsonify({
                "status": "error",
                "message": f"Image file not found on disk: {latest_image['path']}"
            }), 404
            
        return send_file(latest_image["path"], mimetype='image/jpeg')
    except Exception as e:
        logger.error(f"Latest image retrieval failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/images', methods=['GET'])
def get_all_images():
    """Retrieve metadata for all captured images."""
    try:
        db = get_mongo_connection()
        images_collection = db["images"]
        
        images = []
        for img in images_collection.find().sort("timestamp", pymongo.DESCENDING):
            img["_id"] = str(img["_id"])
            img["timestamp"] = img["timestamp"].isoformat()
            images.append(img)
        
        return jsonify({
            "status": "success",
            "total_images": len(images),
            "images": images
        })
    except Exception as e:
        logger.error(f"Image list retrieval failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/image/<image_id>', methods=['GET'])
def get_image_by_id(image_id):
    """Retrieve a specific image by its MongoDB ID."""
    try:
        db = get_mongo_connection()
        images_collection = db["images"]
        
        image = images_collection.find_one({"_id": ObjectId(image_id)})
        if not image:
            return jsonify({"status": "error", "message": "Image not found"}), 404
        
        if not os.path.exists(image["path"]):
            return jsonify({
                "status": "error",
                "message": f"Image file not found on disk: {image['path']}"
            }), 404
            
        return send_file(image["path"], mimetype='image/jpeg')
    except Exception as e:
        logger.error(f"Image retrieval failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    logger.info(f"Starting Smart Fridge Camera Server on port {SERVER_PORT}")
    logger.info(f"Image retention: {IMAGE_RETENTION_DAYS} days")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False)
