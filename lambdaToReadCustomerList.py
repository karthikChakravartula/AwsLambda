import json
import boto3
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
import logging
from datetime import datetime
import uuid
import re

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

def lambda_handler(event, context):
    """
    Lambda function to process customer.xlsx files from S3 and insert data into DynamoDB
    """
    try:
        # Process each record in the event
        for record in event['Records']:
            # Get bucket and object key from the event
            bucket_name = record['s3']['bucket']['name']
            object_key = record['s3']['object']['key']
            
            logger.info(f"Processing file: {object_key} from bucket: {bucket_name}")
            
            # Check if the file is customer.xlsx (case-insensitive) to make sure only relevant file is read and ignore remaining
            if not object_key.lower().endswith('customer.xlsx'):
                logger.info(f"Ignoring file {object_key} - not customer.xlsx")
                continue
            
            # Download and process the Excel file
            process_excel_file(bucket_name, object_key)
        
        return {
            'statusCode': 200,
            'body': json.dumps('Successfully processed customer.xlsx file(s)')
        }
    
    except Exception as e:
        logger.error(f"Error processing file: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Error: {str(e)}')
        }

def process_excel_file(bucket_name, object_key):
    """
    Download Excel file from S3 and process each row 
    """
    try:
        # Download the file from S3
        logger.info(f"Downloading {object_key} from S3")
        response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        excel_content = response['Body'].read()
        
        # Parse Excel file 
        data = parse_xlsx_file(excel_content)
        
        if not data:
            logger.warning("No data found in Excel file")
            return
        
        # Process the data
        process_excel_data(data)
        
    except Exception as e:
        logger.error(f"Error processing Excel file: {str(e)}")
        raise

def parse_xlsx_file(excel_content):
    """
    Parse xlsx file (xlsx is a zip file with XML)
    """
    try:
        # Excel file is a zip archive
        with zipfile.ZipFile(BytesIO(excel_content), 'r') as zip_file:
            # Read shared strings (for text values)
            shared_strings = []
            try:
                shared_strings_xml = zip_file.read('xl/sharedStrings.xml')
                root = ET.fromstring(shared_strings_xml)
                for si in root.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'):
                    t = si.find('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')
                    if t is not None:
                        shared_strings.append(t.text or '')
                    else:
                        shared_strings.append('')
            except:
                logger.info("No shared strings found")
            
            # Read the first worksheet
            worksheet_xml = zip_file.read('xl/worksheets/sheet1.xml')
            root = ET.fromstring(worksheet_xml)
            
            # Extract data from worksheet
            rows_data = []
            for row in root.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row'):
                row_data = []
                for cell in row.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'):
                    cell_value = ""
                    value_elem = cell.find('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                    
                    if value_elem is not None:
                        cell_type = cell.get('t', '')
                        if cell_type == 's':  # Shared string
                            try:
                                string_index = int(value_elem.text)
                                if string_index < len(shared_strings):
                                    cell_value = shared_strings[string_index]
                            except:
                                cell_value = value_elem.text or ""
                        else:
                            cell_value = value_elem.text or ""
                    
                    row_data.append(cell_value)
                
                if row_data:  # Only add non-empty rows
                    rows_data.append(row_data)
            
            return rows_data
            
    except Exception as e:
        logger.error(f"Error parsing Excel file: {str(e)}")
        raise

def process_excel_data(data):

    if not data:
        return
    
    # First row should be headers
    headers = [str(cell).lower().strip() for cell in data[0]]
    logger.info(f"Excel headers: {headers}")
    
    # Find product column index
    try:
        product_col_index = headers.index('product')
    except ValueError:
        raise ValueError("'product' column not found in Excel file")
    
    # Process data rows
    processed_count = 0
    skipped_count = 0
    
    for row_num, row in enumerate(data[1:], start=2):  # Skip header row
        try:
            # Skip empty rows
            if not any(str(cell).strip() for cell in row):
                continue
            
            # Create row dictionary
            row_data = {}
            for col_index, cell_value in enumerate(row):
                if col_index < len(headers) and headers[col_index]:
                    if cell_value and str(cell_value).strip():
                        row_data[headers[col_index]] = str(cell_value).strip()
            
            # Get product value
            product = row_data.get('product', '').lower().strip()
            
            if product == 'aws':
                insert_to_dynamodb('AwsSalesTeam', row_data)
                processed_count += 1
            elif product == 'amazon':
                insert_to_dynamodb('AmazonSalesTeam', row_data)
                processed_count += 1
            else:
                logger.warning(f"Row {row_num}: Unknown product '{product}' - skipping")
                skipped_count += 1
                
        except Exception as e:
            logger.error(f"Error processing row {row_num}: {str(e)}")
            skipped_count += 1
    
    logger.info(f"Processing complete: {processed_count} rows processed, {skipped_count} rows skipped")

def insert_to_dynamodb(table_name, row_data):
    """
    Insert row data into existing DynamoDB table
    """
    try:
        table = dynamodb.Table(table_name)
        
        # Prepare item for DynamoDB
        item = {}
        
        # Copy all row data
        for key, value in row_data.items():
            if value and str(value).strip():  # Skip empty values
                item[key] = value
        
        # Add metadata (adjust based on your existing table schema)
        if 'id' not in item:
            item['CustomerID'] = str(uuid.uuid4())  # Generate unique ID if not present
        item['processed_at'] = datetime.utcnow().isoformat()
        
        # Insert into existing DynamoDB table
        table.put_item(Item=item)
        logger.info(f"Successfully inserted record into existing table {table_name}")
        
    except Exception as e:
        logger.error(f"Error inserting into existing table {table_name}: {str(e)}")
        raise