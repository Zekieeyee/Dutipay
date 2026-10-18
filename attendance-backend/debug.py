import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

print("=" * 50)
print("DEBUG: Supabase Connection Test")
print("=" * 50)

print(f"URL: {url}")
print(f"Key: {key[:20]}...")

try:
    supabase = create_client(url, key)
    print("✅ Supabase client created!")
    
    # Test 1: List all tables
    print("\n--- Test 1: List Tables ---")
    response = supabase.table('employees').select('*').execute()
    print(f"✅ employees table query successful!")
    print(f"   Employees found: {len(response.data)}")
    
    if len(response.data) > 0:
        print("\n--- Employee Data ---")
        for emp in response.data:
            print(f"  - {emp.get('employee_code', 'N/A')}: {emp.get('first_name', 'N/A')} {emp.get('last_name', 'N/A')}")
    else:
        print("   ⚠️ No data found in employees table")
    
    # Test 2: Try different table name just in case
    print("\n--- Test 2: Check other tables ---")
    
    try:
        response = supabase.table('employee').select('*').limit(1).execute()
        print("✅ 'employee' table exists (singular)")
    except Exception as e:
        print("❌ 'employee' table does not exist (singular)")
    
    try:
        response = supabase.table('Employee').select('*').limit(1).execute()
        print("✅ 'Employee' table exists (capitalized)")
    except Exception as e:
        print("❌ 'Employee' table does not exist (capitalized)")
    
    # Test 3: Raw SQL query
    print("\n--- Test 3: Raw SQL Query ---")
    try:
        response = supabase.rpc('sql', {'query': 'SELECT * FROM employees LIMIT 5'})
        print(f"✅ Raw SQL successful! Rows found: {len(response.data)}")
    except Exception as e:
        print(f"❌ Raw SQL failed: {e}")
    
    print("\n" + "=" * 50)
    print("✅ DEBUG COMPLETE")
    print("=" * 50)
    
except Exception as e:
    print(f"❌ ERROR: {e}")
    import traceback
    traceback.print_exc()