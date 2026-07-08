import sys
import os
import psycopg2

# Resolve paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from db_config import DB_CONFIG

def fix_table_constraints(conn):
    cur = conn.cursor()
    
    # List of tables, their unique key columns, and constraint name
    migrations = [
        {
            "table": "calendar",
            "columns": ["date"],
            "constraint": "calendar_date_unique"
        },
        {
            "table": "daily_price",
            "columns": ["date", "symbol"],
            "constraint": "daily_price_date_symbol_unique"
        },
        {
            "table": "nepse_index",
            "columns": ["date", "index_name"],
            "constraint": "nepse_index_date_index_name_unique"
        },
        {
            "table": "sub_index",
            "columns": ["date", "index_name"],
            "constraint": "sub_index_date_index_name_unique"
        },
        {
            "table": "computed_indicators",
            "columns": ["date", "symbol"],
            "constraint": "computed_indicators_date_symbol_unique"
        },
        {
            "table": "companies",
            "columns": ["symbol"],
            "constraint": "companies_symbol_unique"
        },
        {
            "table": "floorsheet",
            "columns": ["contract_no"],
            "constraint": "floorsheet_contract_no_unique"
        }
    ]
    
    for mig in migrations:
        table = mig["table"]
        cols = mig["columns"]
        constraint = mig["constraint"]
        cols_str = ", ".join(cols)
        
        print(f"\nProcessing table: {table} ({cols_str})")
        
        # 1. Check if table exists
        cur.execute("SELECT to_regclass(%s);", (table,))
        if not cur.fetchone()[0]:
            print(f"  Table '{table}' does not exist. Skipping.")
            continue
            
        # 2. Check if a unique constraint or index already exists for these columns
        cols_check_query = """
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            WHERE t.relname = %s AND c.contype IN ('p', 'u')
        """
        cur.execute(cols_check_query, (table,))
        existing_constraints = [row[0] for row in cur.fetchall()]
        
        # Let's check using pg_indexes as well to see if there's a unique index
        cur.execute("""
            SELECT indexname FROM pg_indexes 
            WHERE tablename = %s AND indexdef LIKE '%%UNIQUE%%';
        """, (table,))
        existing_unique_indexes = [row[0] for row in cur.fetchall()]
        
        if existing_constraints or existing_unique_indexes:
            print(f"  Existing constraints/indices found: {existing_constraints + existing_unique_indexes}")
            
        # 3. Clean up duplicate rows before creating the unique constraint
        where_conditions = " AND ".join([f"a.{col} = b.{col}" for col in cols])
        null_conditions = " AND ".join([f"a.{col} IS NOT NULL" for col in cols])
        
        dup_delete_query = f"""
            DELETE FROM {table} a USING {table} b
            WHERE a.ctid < b.ctid AND {where_conditions} AND {null_conditions};
        """
        
        try:
            print("  Checking and cleaning duplicate rows...")
            cur.execute(dup_delete_query)
            deleted_rows = cur.rowcount
            if deleted_rows > 0:
                print(f"  Cleaned up {deleted_rows} duplicate rows.")
            else:
                print("  No duplicate rows found.")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  Error cleaning duplicates for {table}: {e}")
            continue

        # 4. Add the unique constraint
        add_constraint_query = f"""
            ALTER TABLE {table} ADD CONSTRAINT {constraint} UNIQUE ({cols_str});
        """
        try:
            print(f"  Adding UNIQUE constraint '{constraint}'...")
            cur.execute(add_constraint_query)
            conn.commit()
            print("  Constraint added successfully.")
        except psycopg2.errors.DuplicateTable:
            conn.rollback()
            print(f"  Constraint '{constraint}' already exists.")
        except psycopg2.errors.DuplicateObject:
            conn.rollback()
            print(f"  Constraint '{constraint}' already exists.")
        except Exception as e:
            conn.rollback()
            err_str = str(e).lower()
            if "already exists" in err_str:
                print("  Constraint/Index already exists.")
            else:
                print(f"  Failed to add constraint: {e}")

    cur.close()

def main():
    print("=" * 60)
    print("PostgreSQL Database Constraint Optimizer")
    print("=" * 60)
    
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print("Connected to database successfully.")
        fix_table_constraints(conn)
        conn.close()
        print("\nAll database constraints are now successfully configured and verified!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FATAL] Database connection failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
