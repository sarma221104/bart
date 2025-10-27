#!/usr/bin/env python3
"""
AWS Transcribe Custom Vocabulary Setup Script
Creates a custom vocabulary for BART-related terms to improve transcription accuracy.
This script should be run once to set up the vocabulary in AWS Transcribe.
"""

import boto3
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_vocabulary_table_content():
    """Create the vocabulary table content with comprehensive BART mappings"""
    
    # Comprehensive list of common mis-transcriptions of BART (no duplicates)
    bart_mis_transcriptions = [
        # Core mis-transcriptions
        "bat", "bad", "bath", "bark", "burt", "bert", "bart", "b-a-r-t",
        "bar",  # Added as requested (removed "bird" to avoid conflict with "Early Bird Express")
        
        # Variations with 't' suffix
        "batt", "badt", "barth", "barkt", "burtt", "bertt", "bartt",
        
        # Variations with double vowels
        "baat", "baad", "baath", "baark", "baurt", "baert", "baart",
        
        # 'e' sound variations
        "beet", "beat", "bead", "beath", "beak", "beurt", "beert", "beart",
        
        # 'o' sound variations
        "bot", "bod", "both", "bork", "bort",
        
        # 'u' sound variations
        "but", "bud", "buth", "burk",
        
        # 'i' sound variations
        "bit", "bid", "bith", "birk", "birt",
        
        # 'e' sound variations (different)
        "bet", "bed", "beth", "berk",
        
        # 'ai' sound variations
        "bait", "baid", "baith", "baik", "bairt",
        
        # 'oa' sound variations
        "boat", "boad", "boath", "boak", "boart",
        
        # 'oo' sound variations
        "boot", "bood", "booth", "book", "boort",
        
        # 'ou' sound variations
        "bout", "boud", "bouth", "bouk", "bourt"
    ]
    
    # Create table content with headers
    table_lines = ["Phrase\tSoundsLike\tIPA\tDisplayAs"]
    
    # Use a set to track unique phrases and avoid duplicates
    unique_phrases = set()
    
    # Add mappings for each mis-transcription (no duplicates)
    for mis_transcription in bart_mis_transcriptions:
        # Clean the phrase (remove spaces, convert to lowercase for phrase column)
        phrase = mis_transcription.lower().replace(" ", "-").replace(".", "")
        
        # Skip if we've already added this phrase
        if phrase in unique_phrases:
            continue
            
        unique_phrases.add(phrase)
        display_as = "BART"  # All should display as BART
        
        # Create table row: Phrase[TAB]SoundsLike[TAB]IPA[TAB]DisplayAs
        table_lines.append(f"{phrase}\t\t\t{display_as}")
    
    # Add BART-related terms (no duplicates)
    additional_terms = [
        ("bart-system", "BART System"),
        ("bart-train", "BART Train"),
        ("bart-station", "BART Station"),
        ("bart-route", "BART Route"),
        ("bart-line", "BART Line"),
        ("bart-service", "BART Service"),
        ("bart-commute", "BART Commute"),
        ("bart-ride", "BART Ride"),
        ("bart-trip", "BART Trip"),
        ("bart-fare", "BART Fare"),
        ("bart-ticket", "BART Ticket"),
        ("bart-pass", "BART Pass"),
        ("bart-card", "BART Card"),
        ("bart-clipper", "BART Clipper"),
        ("bart-bay-area", "BART Bay Area"),
        ("bart-transit", "BART  Transit")
    ]
    
    for phrase, display_as in additional_terms:
        # Skip if we've already added this phrase
        if phrase in unique_phrases:
            continue
            
        unique_phrases.add(phrase)
        table_lines.append(f"{phrase}\t\t\t{display_as}")
    
    return "\n".join(table_lines)

def create_s3_bucket_if_not_exists(bucket_name: str, region: str = 'us-west-2'):
    """Create S3 bucket if it doesn't exist"""
    
    s3_client = boto3.client('s3', region_name=region)
    
    try:
        # Check if bucket exists
        s3_client.head_bucket(Bucket=bucket_name)
        logger.info(f"S3 bucket '{bucket_name}' already exists")
        return True
    except s3_client.exceptions.NoSuchBucket:
        # Bucket doesn't exist, create it
        try:
            if region == 'us-west-2':
                # us-west-2 doesn't need LocationConstraint
                s3_client.create_bucket(Bucket=bucket_name)
            else:
                # All other regions (including us-west-2) need LocationConstraint
                s3_client.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={'LocationConstraint': region}
                )
            logger.info(f"S3 bucket '{bucket_name}' created successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to create S3 bucket '{bucket_name}': {str(e)}")
            return False
    except Exception as e:
        logger.error(f"Error checking S3 bucket '{bucket_name}': {str(e)}")
        return False

def create_bart_custom_vocabulary():
    """Create a custom vocabulary in AWS Transcribe for BART-related terms using table format"""
    
    # Initialize AWS Transcribe client
    transcribe_client = boto3.client('transcribe', region_name='us-west-2')
    
    # Prepare vocabulary data
    vocabulary_name = "BART_Custom_Vocabulary"
    language_code = "en-US"
    
    # Create vocabulary table content (tab-separated)
    # Format: Phrase[TAB]SoundsLike[TAB]IPA[TAB]DisplayAs
    vocabulary_content = create_vocabulary_table_content()
    
    # Upload to S3 first (required for table format)
    s3_bucket = "bart-transcribe-vocabulary"
    s3_key = "bart-vocabulary-table.txt"
    
    # Verify S3 bucket exists (you've already created it)
    if not create_s3_bucket_if_not_exists(s3_bucket):
        logger.error("Failed to access S3 bucket - please ensure it exists")
        return False
    
    try:
        # Upload vocabulary file to S3
        s3_client = boto3.client('s3', region_name='us-west-2')
        s3_client.put_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Body=vocabulary_content,
            ContentType='text/plain'
        )
        
        s3_uri = f"s3://{s3_bucket}/{s3_key}"
        logger.info(f"Vocabulary file uploaded to S3: {s3_uri}")
        
        # Create the custom vocabulary using S3 URI
        response = transcribe_client.create_vocabulary(
            VocabularyName=vocabulary_name,
            LanguageCode=language_code,
            VocabularyFileUri=s3_uri
        )
        
        logger.info(f"Custom vocabulary '{vocabulary_name}' created successfully")
        logger.info(f"Vocabulary State: {response.get('VocabularyState', 'Unknown')}")
        
        return True
        
    except transcribe_client.exceptions.ConflictException:
        logger.warning(f"Vocabulary '{vocabulary_name}' already exists")
        return True
        
    except Exception as e:
        logger.error(f"Failed to create custom vocabulary: {str(e)}")
        return False

def update_bart_custom_vocabulary():
    """Update an existing custom vocabulary using table format"""
    
    transcribe_client = boto3.client('transcribe', region_name='us-west-2')
    s3_client = boto3.client('s3', region_name='us-west-2')
    
    vocabulary_name = "BART_Custom_Vocabulary"
    language_code = "en-US"
    s3_bucket = "bart-transcribe-vocabulary"
    s3_key = "bart-vocabulary-table.txt"
    
    # Create updated vocabulary table content
    vocabulary_content = create_vocabulary_table_content()
    
    # Verify S3 bucket exists (you've already created it)
    if not create_s3_bucket_if_not_exists(s3_bucket):
        logger.error("Failed to access S3 bucket - please ensure it exists")
        return False
    
    try:
        # Upload updated vocabulary file to S3
        s3_client.put_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Body=vocabulary_content,
            ContentType='text/plain'
        )
        
        s3_uri = f"s3://{s3_bucket}/{s3_key}"
        logger.info(f"Updated vocabulary file uploaded to S3: {s3_uri}")
        
        # Update the custom vocabulary using S3 URI
        response = transcribe_client.update_vocabulary(
            VocabularyName=vocabulary_name,
            LanguageCode=language_code,
            VocabularyFileUri=s3_uri
        )
        
        logger.info(f"Custom vocabulary '{vocabulary_name}' updated successfully")
        logger.info(f"Vocabulary State: {response.get('VocabularyState', 'Unknown')}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to update custom vocabulary: {str(e)}")
        return False

def get_vocabulary_status():
    """Get the status of the custom vocabulary"""
    
    transcribe_client = boto3.client('transcribe', region_name='us-west-2')
    vocabulary_name = "BART_Custom_Vocabulary"
    
    try:
        response = transcribe_client.get_vocabulary(VocabularyName=vocabulary_name)
        
        logger.info(f"Vocabulary Status: {response.get('VocabularyState', 'Unknown')}")
        logger.info(f"Language Code: {response.get('LanguageCode', 'Unknown')}")
        logger.info(f"Last Modified: {response.get('LastModifiedTime', 'Unknown')}")
        
        return response
        
    except transcribe_client.exceptions.NotFoundException:
        logger.warning(f"Vocabulary '{vocabulary_name}' not found")
        return None
        
    except Exception as e:
        logger.error(f"Failed to get vocabulary status: {str(e)}")
        return None

def delete_bart_custom_vocabulary():
    """Delete the custom vocabulary (for cleanup)"""
    
    transcribe_client = boto3.client('transcribe', region_name='us-west-2')
    vocabulary_name = "BART_Custom_Vocabulary"
    
    try:
        transcribe_client.delete_vocabulary(VocabularyName=vocabulary_name)
        logger.info(f"Custom vocabulary '{vocabulary_name}' deleted successfully")
        return True
        
    except transcribe_client.exceptions.NotFoundException:
        logger.warning(f"Vocabulary '{vocabulary_name}' not found")
        return True
        
    except Exception as e:
        logger.error(f"Failed to delete custom vocabulary: {str(e)}")
        return False

def main():
    """Main function to set up AWS Transcribe custom vocabulary"""
    
    print("Setting up Enhanced BART Custom Vocabulary for AWS Transcribe...")
    print("=" * 70)
    
    # Enhanced BART vocabulary stats
    vocabulary_content = create_vocabulary_table_content()
    total_entries = len(vocabulary_content.split('\n')) - 1  # Subtract header row
    
    print(f"📊 Vocabulary Statistics:")
    print(f"   • Total vocabulary entries: {total_entries}")
    print(f"   • Common mis-transcriptions: 50+")
    print(f"   • BART-related terms: 16")
    print(f"   • Format: AWS Transcribe Table Format")
    print(f"   • S3 Bucket: bart-transcribe-vocabulary (us-west-2)")
    print()
    
    # Check if vocabulary already exists
    status = get_vocabulary_status()
    
    if status:
        print("🔄 Vocabulary already exists. Updating with enhanced mappings...")
        success = update_bart_custom_vocabulary()
    else:
        print("🆕 Creating new enhanced vocabulary...")
        success = create_bart_custom_vocabulary()
    
    if success:
        print("\n✅ Enhanced BART Custom Vocabulary setup completed successfully!")
        print("\n🎯 The vocabulary will now correctly transcribe:")
        print("   • 'bat', 'bad', 'bath', 'bark', 'burt', 'bert' → 'BART'")
        print("   • 'beet', 'beat', 'bead', 'beath' → 'BART'")
        print("   • 'bot', 'bod', 'both', 'bork' → 'BART'")
        print("   • 'but', 'bud', 'buth', 'burk' → 'BART'")
        print("   • 'bit', 'bid', 'bith', 'birk' → 'BART'")
        print("   • 'bet', 'bed', 'beth', 'berk' → 'BART'")
        print("   • And 40+ more common mis-transcriptions")
        print("\n🚀 BART-related terms will be properly recognized:")
        print("   • 'BART System', 'BART Train', 'BART Station'")
        print("   • 'BART Route', 'BART Line', 'BART Service'")
        print("   • 'BART Commute', 'BART Ride', 'BART Trip'")
        print("   • And more BART-specific terminology")
        print("\n📝 Note: This uses AWS Transcribe's table format for maximum accuracy")
    else:
        print("\n❌ Failed to set up Enhanced BART Custom Vocabulary")
        print("The system will use post-processing fallback instead")

if __name__ == "__main__":
    main()
