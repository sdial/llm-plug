import asyncio
import json
import os
import asyncpg

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://localhost:5432/llmplug")

async def test():
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS test_jsonb")
        await conn.execute("CREATE TABLE test_jsonb (data JSONB)")
        
        # Method 1: json.dumps string
        await conn.execute("INSERT INTO test_jsonb VALUES ($1)", json.dumps({"a": 1}))
        row = await conn.fetchrow("SELECT * FROM test_jsonb")
        print(f"Method 1 (string): {type(row['data'])} = {row['data']!r}")
        
        await conn.execute("DELETE FROM test_jsonb")
        
        # Method 2: json.dumps string with ::jsonb
        await conn.execute("INSERT INTO test_jsonb VALUES ($1::jsonb)", json.dumps({"a": 1}))
        row = await conn.fetchrow("SELECT * FROM test_jsonb")
        print(f"Method 2 (string::jsonb): {type(row['data'])} = {row['data']!r}")
        
        await conn.execute("DELETE FROM test_jsonb")
        
        # Method 3: dict directly
        try:
            await conn.execute("INSERT INTO test_jsonb VALUES ($1)", {"a": 1})
            row = await conn.fetchrow("SELECT * FROM test_jsonb")
            print(f"Method 3 (dict): {type(row['data'])} = {row['data']!r}")
        except Exception as e:
            print(f"Method 3 (dict) failed: {e}")
        
        await conn.execute("DELETE FROM test_jsonb")
        
        # Method 4: dict with ::jsonb
        try:
            await conn.execute("INSERT INTO test_jsonb VALUES ($1::jsonb)", {"a": 1})
            row = await conn.fetchrow("SELECT * FROM test_jsonb")
            print(f"Method 4 (dict::jsonb): {type(row['data'])} = {row['data']!r}")
        except Exception as e:
            print(f"Method 4 (dict::jsonb) failed: {e}")
        
    await pool.close()

asyncio.run(test())
