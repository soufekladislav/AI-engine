from app.worker import process_batch

if __name__ == "__main__":
    count = process_batch()
    print(f"processed={count}")
