from googlesearch import search

query = "cars in Montreal"
for url in search(query, num_results=5):
    print(url)