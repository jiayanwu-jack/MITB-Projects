# Applied Data Science in Social Networks - Open Book Exam Reference Notes

Scope: Sessions 1-8 lecture PDFs, summary/FAQ PDFs, and lab tutorial notebooks. Lab exercises and solutions are intentionally excluded.

## 0. Exam Strategy

- For calculation questions: write the definition first, identify the graph type, then compute carefully from node/edge sets.
- For interpretation questions: always map the metric to the social meaning. Example: high betweenness means a bridge/control position, not necessarily popularity.
- For method-choice questions: no single best metric/model. Choice depends on the task, graph type, scalability, and whether the target is node influence, node similarity, text meaning, or community structure.
- Common traps: directed vs undirected, weighted vs unweighted, self-pairs in shortest-path averages, degree count vs normalized centrality, common neighbors vs similar neighbors, clustering coefficient vs modularity.

## 1. Graph Fundamentals

### Core Definitions

- Graph: `G = (V, E)` where `V` is the vertex/node set and `E` is the edge set.
- Undirected edge: unordered pair `{u, v}`.
- Directed edge: ordered pair `(u, v)` or `u -> v`.
- Weighted edge: edge has weight, usually relationship strength, frequency, cost, or distance.
- Adjacent/neighbors: `u` and `v` are adjacent if an edge connects them.
- Degree: number of incident edges. In directed graphs:
  - in-degree: number of incoming edges.
  - out-degree: number of outgoing edges.
- Complete graph: every pair of nodes is connected. For `n` nodes, undirected edges = `n(n-1)/2`.
- Clique: subset of nodes that are pairwise adjacent.
- Walk: sequence of nodes where consecutive nodes are adjacent; repeated nodes/edges allowed.
- Path: walk with distinct vertices, except possibly start=end depending on convention.
- Cycle: path that starts and ends at the same node.
- Connected component: maximal set of nodes mutually reachable from each other.

### Graph Representations

- Adjacency matrix `A`: `A_ij = 1` if edge from `i` to `j`, else `0`; can store weights instead of `1`.
- Undirected graph adjacency matrix is symmetric.
- Directed graph adjacency matrix is not necessarily symmetric.
- Edge list: one edge per row, e.g. `u v` or `u v weight`.
- Adjacency list: each node followed by its neighbors.

### NetworkX Basics

```python
import networkx as nx
import matplotlib.pyplot as plt

g = nx.Graph()        # undirected
dg = nx.DiGraph()     # directed

g.add_node(0)
g.add_nodes_from([1, 2, 3])
g.add_edge(0, 1)
g.add_edges_from([(1, 2), (2, 3)])
g.add_edge("a", "b", weight=3.5)

g.number_of_nodes()
g.number_of_edges()
g.nodes
g.edges
g.degree(1)
list(g.neighbors(1))
g[1]                  # adjacency dict for node 1
g[u][v]["weight"]     # edge weight

nx.draw(g, with_labels=True)
nx.write_gexf(g, "graph.gexf")  # for Gephi
```

Notes:
- Adding an existing node/edge again does not duplicate it.
- Adding an edge between new nodes automatically adds those nodes.
- Adding an existing weighted edge with a new weight updates the weight.

## 2. Social Networks as Graphs

### Common Social Graph Types

- User-user graph: friendships, follows, replies, mentions.
- User-item bipartite graph: users-products, users-locations, users-posts, actors-movies.
- Heterogeneous information network: multiple node and edge types, e.g. users, photos, locations; likes, follows, tags.
- Co-occurrence graph: edge exists when two items co-occur, e.g. words in same window, products co-purchased.
- Ego-network: subgraph formed by a focal node (ego) and its neighbors.

### Bipartite Graph

- Nodes partition into two disjoint sets `U` and `V`.
- Every edge has one endpoint in each set.
- No edges within the same partition.
- Clustering coefficient of a purely bipartite graph is always zero because it has no triangles.

### Ego-Network

If ego degree is `d`:
- Number of nodes in ego-network = `d + 1`.
- Minimum number of edges = `d` (ego connected to each neighbor only).
- Maximum number of edges = `d + d(d-1)/2 = d(d+1)/2`.

NetworkX:

```python
egonet = nx.ego_graph(g, ego_node)
```

### Exploratory Network Analysis

Social networks often show:
- Small-world property: short paths between most pairs; average distance grows roughly with `log(N)`.
- Power-law-like degree distribution: many low-degree nodes, few very high-degree nodes.
- High clustering: friends of a user are likely to be friends.
- Preferential attachment: new nodes prefer connecting to already popular/high-degree nodes.
- Homophily: similar nodes are more likely to connect.

### Network Diameter

Diameter is a summary statistic of shortest-path distances among node pairs. Common versions:
- Average shortest-path distance.
- Maximum shortest-path distance.
- 90th percentile shortest-path distance.

Important lab trap:
- `nx.all_pairs_shortest_path_length(g)` includes self-pairs with distance `0`.
- Exclude self-pairs for average distance if the intended definition is average among distinct node pairs.
- Self-pairs do not affect max; they may usually remain for percentile unless specified.

```python
import numpy as np

all_pair = dict(nx.all_pairs_shortest_path_length(g))
distances = []
for s in all_pair:
    for t in all_pair[s]:
        if s != t:
            distances.append(all_pair[s][t])

avg_d = np.mean(distances)
max_d = np.max(distances)
p90_d = np.percentile(distances, 90)
```

For very large graphs, sample node pairs and estimate shortest-path statistics.

### Degree Distribution

```python
degrees = sorted([g.degree(n) for n in g.nodes()], reverse=True)
plt.plot(degrees)
plt.xlabel("Rank of nodes by degree")
plt.ylabel("Node degree")
```

Power-law intuition: a small number of "celebrity" nodes have very high degrees; most nodes have low degree.

### Clustering Coefficient / Transitivity

Global clustering coefficient:

```text
C = number of closed triplets / number of all triplets
  = 3 * number of triangles / number of all triplets
```

NetworkX:

```python
c = nx.transitivity(g)
```

Interpretation: in a friendship graph, `C = 0.519` means two users sharing a common friend have about a 51.9% chance of being connected.

### Homophily Ratio

```text
h = number of edges between nodes of the same class / total number of edges
```

```python
def homophily_ratio(g, classes):
    same = 0
    for u, v in g.edges():
        if classes[u] == classes[v]:
            same += 1
    return same / g.number_of_edges()
```

High `h` means strong tendency for linked nodes to share the selected attribute/class. Homophily depends on the chosen class definition.

## 3. Social APIs and Data Collection

### API Concepts

- API = Application Programming Interface.
- Endpoint = URL/location for a request.
- Request parameters control what data are requested.
- Response usually contains status code plus data, often JSON.
- Authentication verifies identity; authorization determines access rights.
- API rules/rate limits matter; abusive/aggressive calling can get accounts or IPs blocked.

### Common HTTP Status Codes

- `200`: success.
- `401`: unauthorized; missing/incorrect credentials.
- `403`: forbidden; credentials accepted but insufficient privilege.
- `404`: not found; bad endpoint/resource.
- `500`: server error.

### Reddit API Tutorial Pattern

```python
import requests

auth = requests.auth.HTTPBasicAuth(my_id, my_secret)
res = requests.post(
    "https://www.reddit.com/api/v1/access_token",
    auth=auth,
    data=login_info,
    headers=headers
)

response = requests.get("https://oauth.reddit.com/api/v1/me", headers=headers)
response.status_code
response.json()
```

Data types from APIs:
- Node data: user profile, subreddit metadata.
- Edge data: user-post, user-subreddit, user-user comments/replies.
- Text data: post titles, selftext, comments, search results.

Useful Reddit endpoints from lab:
- User info: `user/[USERNAME]/about`.
- Subreddit info: `r/[SUBREDDIT]/about`.
- New/hot posts: `r/[SUBREDDIT]/new`, `r/[SUBREDDIT]/hot`.
- Comments: `comments/[POST_ID]`.
- Search: `search`, or `r/[SUBREDDIT]/search` with `restrict_sr=on`.
- Pagination: use `after` parameter returned by current response.

```python
content = reddit_api_call("r/ChatGPT/new", params={"limit": 5})
after = content["data"]["after"]
content = reddit_api_call("r/ChatGPT/new", params={"limit": 5, "after": after})
```

## 4. Social Text Analytics

### Text Data in Social Networks

Examples: status updates, comments, reviews, tweets/posts, Q&A/discussions, profiles.

Main tasks:
- Document similarity.
- Sentiment analysis.
- Topic modeling.

### Vector Space Model / Bag of Words

- Corpus: set of documents.
- Vocabulary: set of words appearing in corpus.
- Each word is a dimension.
- Each document is a vector of term frequencies.
- Very high-dimensional and sparse.
- Bag of words ignores word order; it stores frequencies only.

Term frequency:

```text
tf(word, doc) = count of word in document
```

Dot product:

```text
d1 dot d2 = sum_i d1_i * d2_i
```

Dot product can be misleading because longer documents have more chances to share words.

Cosine similarity:

```text
cosine(d1, d2) = (d1 dot d2) / (||d1|| * ||d2||)
```

where `||d|| = sqrt(sum_i d_i^2)`.

TF-IDF:

```text
tf-idf = tf * idf
```

Intuition: frequent words within a document matter, but words appearing in many documents are less discriminative.

### Text Preprocessing Pipeline

1. Case normalization: lowercase.
2. Remove corpus-specific noise, e.g. HTML `<br />`.
3. Tokenize text.
4. Remove non-word tokens if appropriate.
5. Remove stop words.
6. Unify word forms:
   - Stemming: fast, rule-based, crude.
   - Lemmatization: slower, usually more precise, may need part-of-speech context.

Lab caveat: in short social messages, punctuation/emoticons may carry sentiment and should not always be removed.

```python
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
from nltk.stem.porter import PorterStemmer

corpus = [doc.lower() for doc in corpus_raw]
corpus = [doc.replace("<br />", "\n") for doc in corpus]
corpus = [word_tokenize(doc) for doc in corpus]
corpus = [[tok for tok in doc if tok[0].isalpha()] for doc in corpus]

stop_set = set(stopwords.words("english"))
corpus = [[tok for tok in doc if tok not in stop_set] for doc in corpus]

stemmer = PorterStemmer()
corpus = [[stemmer.stem(tok) for tok in doc] for doc in corpus]
```

### Gensim BOW and Similarity

```python
from gensim import corpora
from gensim.similarities import SparseMatrixSimilarity

vocab = corpora.Dictionary(corpus)
vocab.filter_extremes(no_below=3, no_above=0.25)

bow = [vocab.doc2bow(doc) for doc in corpus]  # sparse list of (word_id, tf)

index = SparseMatrixSimilarity(bow, num_terms=len(vocab), num_best=5)
similarities = index[bow[0]]
```

`filter_extremes`:
- `no_below`: remove words appearing in fewer than this many documents.
- `no_above`: remove words appearing in more than this fraction of documents.

### Topic Modeling

Topic = distribution over words.

Document = distribution over topics.

LDA intuition:
- Documents mix topics.
- Topics generate words with different probabilities.
- Choose number of topics using domain knowledge, interpretability, and/or coherence.

```python
from gensim import models

n_topic = 10
lda = models.LdaModel(bow, id2word=vocab, num_topics=n_topic)
for i in range(n_topic):
    print(i, lda.print_topic(i, topn=5))

lda[bow[0]]  # topic distribution for document 0
```

Too many topics: fine-grained but may be noisy/meaningless.
Too few topics: coarse and may merge unrelated themes.

### Sentiment Analysis

Two common approaches:
- Supervised machine learning: train model from labeled examples.
- Lexicon-based: use sentiment dictionary/rules.

VADER:
- Lexicon-based.
- Useful for social media style text.
- Returns `pos`, `neu`, `neg`, and `compound`.
- Compound is in `[-1, 1]`; closer to `1` positive, closer to `-1` negative.
- VADER can take raw text and handles some preprocessing internally.

```python
from nltk.sentiment.vader import SentimentIntensityAnalyzer

sia = SentimentIntensityAnalyzer()
scores = sia.polarity_scores(doc)
```

## 5. Social Influence: Centrality

Centrality measures node importance/influence. Always interpret by use case.

### Degree Centrality

Intuition: node connected to many others.

```text
degree centrality(u) = degree(u) / (|V| - 1)
```

Course slide examples used denominator based on total other possible nodes. In NetworkX, normalized degree centrality is `degree / (n-1)`.

Directed graph:
- In-degree: popularity/authority, e.g. followers, hyperlinks received.
- Out-degree: activity/hub/gregariousness, e.g. follows many, links out.

Use when:
- Need fast result.
- Need direct reach in ego-network.

Weakness:
- Only direct influence; ignores quality or position of neighbors.

### Closeness Centrality

Intuition: how quickly a node can reach others.

```text
closeness(u) = (|V| - 1) / sum_{v != u} d(u, v)
```

Use when:
- Finding individuals who can influence/reach a large proportion of network quickly.

Weakness:
- In small-world networks, many nodes have similar closeness.
- Disconnected graphs require care; unreachable nodes complicate distances.

### Betweenness Centrality

Intuition: how often node lies on shortest paths between other nodes.

```text
betweenness(u) = sum_{s != u != t} sigma_st(u) / sigma_st
```

where:
- `sigma_st` = number of shortest paths from `s` to `t`.
- `sigma_st(u)` = number of those shortest paths passing through `u`.

Use when:
- Finding nodes that control/bridge information between groups.

Weakness:
- High betweenness can mean an important bridge or a peripheral connector between groups.
- Raw betweenness can be greater than 1; normalized versions differ.

### Eigenvector Centrality

Intuition: a node is important if connected to important nodes.

```text
x_u proportional to sum_{v in IN(u)} x_v
A^T x = lambda x
```

Weakness:
- Can behave poorly in directed graphs with sinks/traps.
- Less interpretable than degree.

### PageRank

Variant of eigenvector centrality with link normalization and teleportation.

Basic idea:

```text
x_u = sum_{v in IN(u)} x_v / outdeg(v)
```

With teleportation:

```text
x_u = (1 - alpha) / |V| + alpha * sum_{v in IN(u)} x_v / outdeg(v)
```

Typical `alpha = 0.85`.

Weighted version:

```text
x_u = (1 - alpha) / |V| + alpha * sum_{v in IN(u)} (w_vu * x_v / weighted_outdeg(v))
```

Dangling node: node with no out-links. PageRank treats it as distributing importance broadly, avoiding lost mass.

Local trap: group of pages linking internally. Teleportation gives probability of escaping.

Properties:
- PageRank values sum to 1.
- High in-degree tends to correlate with PageRank, but PageRank also considers the importance of in-neighbors and their out-degree.
- PageRank is not "best"; it is task-dependent.

NetworkX:

```python
nx.degree_centrality(g)
nx.in_degree_centrality(dg)
nx.out_degree_centrality(dg)
nx.closeness_centrality(g)
nx.betweenness_centrality(g, normalized=True)
nx.eigenvector_centrality(g)
nx.pagerank(dg, alpha=0.85)
```

## 6. Social Communities

### Community Structure

Communities are groups/clusters of nodes.

Good communities:
- Dense within-community edges.
- Sparse between-community edges.
- Nodes in same community more likely to link to each other.

Important distinction:
- "No edges between communities and all edges within communities" describes connected components, not ordinary communities.
- Clustering coefficient measures how likely a graph contains community-like closure; it does not evaluate a particular community assignment.

### Hierarchical Community Detection

Two extremes:
- All nodes in one community: too coarse.
- Each node singleton: too fine.

Methods:
- Agglomerative: bottom-up; start with singleton nodes and merge communities.
- Divisive: top-down; start with one community and split.

Stopping criteria:
- Stop when target number of communities obtained.
- Stop when objective, such as modularity, is maximized.

### Modularity

Measures how good a community assignment is by comparing observed within-community edges against expected edges in a randomized graph preserving node degrees.

Expected edges between `i` and `j`:

```text
E_ij = k_i * k_j / (2m)
```

where `k_i`, `k_j` are degrees and `m` is number of undirected edges.

Modularity:

```text
Q = (1 / 2m) * sum_{i,j} [A_ij - (k_i k_j)/(2m)] * delta(s_i, s_j)
```

where:
- `A_ij` = observed adjacency.
- `s_i` = community assignment of node `i`.
- `delta(s_i, s_j) = 1` if same community, else `0`.
- `B_ij = A_ij - k_i k_j/(2m)` is modularity matrix.

Interpretation:
- Higher `Q` means more within-community edges than expected by random degree-preserving rewiring.
- `Q` depends on the community assignment.
- A graph has one clustering coefficient value, but can have many modularity scores for different assignments.

### Algorithms

Modularity-based agglomerative:
- Start from singleton communities.
- Repeatedly merge the pair that increases modularity most.
- Stop when no merge increases modularity.

NetworkX:

```python
from networkx.algorithms.community import greedy_modularity_communities

communities = list(greedy_modularity_communities(g))
```

Centrality-based divisive (Girvan-Newman intuition):
- Bridge edges between communities should have high edge betweenness.
- Repeatedly remove highest edge-betweenness edge.
- Splits network into communities.

```python
from networkx.algorithms.community import girvan_newman

comp = girvan_newman(g)
communities = next(comp)
```

Scalability note:
- Modularity- and centrality-based methods can be expensive on large graphs.
- Label Propagation Algorithm (LPA) is more scalable but may be less stable/less high-quality.

```python
from networkx.algorithms.community import label_propagation_communities

communities = list(label_propagation_communities(g))
```

## 7. Social Similarities

Social similarity/node proximity measures how similar or close two nodes are.

Use cases:
- Friend suggestion.
- Product recommendation.
- Similar-user retrieval.
- Link prediction.

### Jaccard Similarity

Common-neighbor based, normalized by union.

```text
J(a, b) = |N(a) intersect N(b)| / |N(a) union N(b)|
```

Strength:
- Simple, intuitive.
- Corrects for high-degree nodes more than raw common-neighbor count.

Weakness:
- Only considers direct common neighbors.
- If no common neighbors, score is `0`.

NetworkX:

```python
scores = nx.jaccard_coefficient(g, [(u, v)])
```

### Adamic-Adar

Common-neighbor based, but downweights popular common neighbors.

```text
AA(a, b) = sum_{v in N(a) intersect N(b)} 1 / log(|N(v)|)
```

Intuition:
- A rare/specific shared neighbor is more informative than a very popular shared neighbor.
- Uses natural log; log avoids over-penalizing popular nodes too much.

NetworkX:

```python
scores = nx.adamic_adar_index(g, [(u, v)])
```

### SimRank

Recursive intuition:
- Two nodes are similar if they are linked to many similar neighbors.
- Can produce non-zero similarity even when two nodes have no common neighbors, if their neighbors are similar.

Base case:

```text
S(a, a) = 1
```

Recursive case:

```text
S(a, b) = C / (|N(a)| |N(b)|) *
          sum_{a' in N(a)} sum_{b' in N(b)} S(a', b')
```

`C` is damping factor, typically `0.85`.

Iterative implementation:

```text
S_0(a,b) = 1 if a=b, else 0
S_k(a,b) = C / (|N(a)| |N(b)|) *
           sum_{a' in N(a)} sum_{b' in N(b)} S_{k-1}(a', b')
```

Continue until convergence.

Differences from PageRank:
- PageRank computes one score per node; SimRank computes one score per node pair.
- PageRank can start from many initial states; SimRank must start from the base-case similarity matrix.
- SimRank is much more computationally expensive.

NetworkX:

```python
sim = nx.simrank_similarity(g, importance_factor=0.85)
sim[u][v]
```

Directed graph extension:
- Use in-neighbors or out-neighbors depending on semantics; be explicit.

## 8. Word and Network Embedding

### Why Embeddings?

Vector space model issues:
- Very high-dimensional.
- Very sparse.
- Curse of dimensionality: more dimensions require more samples.
- Inadequate semantics: treats synonyms/related words as unrelated dimensions.

Embedding goal:
- Convert words/nodes to low-dimensional dense vectors.
- Similar or related items should be close.
- Dissimilar items should be far.

Too few dimensions:
- Less expressive/model capacity.

Typical word embedding dimensions:
- Often 50-200 for word2vec-style course examples.

### Word2Vec / Skip-Gram

Key intuition:
- Similar/related words appear in similar contexts or together frequently.
- Context words are within a window around a target word.

Skip-gram:
- Slide a window over text.
- Form target-context pairs.
- Optimize vectors so target and context words are close, while negative/random words are farther.

Number of context-target pairs:
- For each target word, count up to `window` words before and `window` after.
- Near sentence boundaries, fewer pairs.
- Example from slides: 20-word sentence, window size 5 gives `170` pairs, not `20 * 10 = 200`, because boundary words have fewer contexts.

Gensim:

```python
from gensim.models import Word2Vec

model = Word2Vec(
    docs,
    vector_size=50,
    window=5,
    min_count=1,
    negative=5,
    workers=4
)

model.save("model.word2vec.model")
model = Word2Vec.load("model.word2vec.model")
model.wv["dollar"]
model.wv.most_similar(positive=["mortgage"], topn=10)
model.wv.most_similar(positive=["dollar", "oil"], topn=10)
```

Important parameters:
- `vector_size`: number of dimensions.
- `window`: context window size.
- `min_count`: discard words below frequency threshold.
- `negative`: number of negative samples.
- `workers`: parallel threads.

Incremental training:

```python
model.train(new_docs, total_examples=len(new_docs), epochs=1)
```

Why stemming may be unnecessary for word embeddings:
- Embeddings can learn related forms if context supports it.
- Aggressive stemming may remove semantic nuance.

Document vector from word embeddings:
- Average/sum word vectors.
- Or use model-specific document embeddings.
- Applications: document clustering, classification, retrieval.

### Network Embedding

Goal:
- Learn dense vectors for graph nodes.
- Preserve network structure so nearby/similar nodes have close vectors.

DeepWalk intuition:
- Generate random walks on graph.
- Treat random walks like sentences and nodes like words.
- Apply word2vec/skip-gram to node sequences.

DeepWalk parameters:
- `--representation-size`: node vector dimensions, often 100-500.
- `--window-size`: context window for nodes.
- `--input`: graph file.
- `--format`: `adjlist` or `edgelist`.
- `--number-walks`: walks sampled from every node.
- `--walk-length`: length of each random walk.
- `--workers`: parallel threads.
- `--output`: learned embedding file.

```bash
deepwalk --input karate.adjlist --format adjlist --output karate.emb \
  --number-walks 50 --walk-length 50 --representation-size 100 \
  --window-size 5 --workers 4
```

Applications:
- Node clustering.
- Node classification.
- Link prediction.
- Similarity search using cosine similarity.

Weighted/directed networks:
- Graph-based methods can be modified to account for weights/directions; specify how walks or neighbor selection change.

## 9. Quick Method Selection Table

| Task | Use | Key Reason |
|---|---|---|
| Directly influential/popular nodes | Degree / in-degree | Simple direct reach or popularity |
| Fast centrality baseline | Degree | Cheap to compute |
| Quickly reach entire network | Closeness | Based on shortest distance to all nodes |
| Brokers/bridges between groups | Betweenness | Counts shortest paths through node |
| Importance from important neighbors | Eigenvector | Recursive importance |
| Web/link authority with traps handled | PageRank | Normalizes outgoing links + teleportation |
| Whether graph is clustered | Clustering coefficient/transitivity | Global triangle closure |
| Find communities | Modularity/Girvan-Newman/LPA | Group dense internal links |
| Evaluate community assignment | Modularity | Observed vs expected within-community edges |
| Similar nodes with common neighbors | Jaccard | Normalized overlap |
| Similar nodes, rare common neighbors matter | Adamic-Adar | Downweights popular common neighbors |
| Similar nodes without common neighbors | SimRank | Similarity through similar neighbors |
| Text similarity | Cosine over BOW/TF-IDF | Length-normalized vector similarity |
| Discover themes | LDA/topic modeling | Topics as word distributions |
| Sentiment polarity | VADER or supervised ML | Lexicon/rules or learned labels |
| Semantic word similarity | Word2Vec | Dense vectors from contexts |
| Node embeddings | DeepWalk | Random walks + word2vec idea |

## 10. Common True/False Style Traps

- A small-world network does not mean diameter must be at most six. Six is a typical intuition, not a hard threshold.
- Clustering coefficient of a bipartite graph is zero; complete graph clustering coefficient is one.
- Preferential attachment is about new nodes preferring popular/high-degree nodes, not similar nodes preferring similar options. Similarity-based linking is homophily.
- Ego-networks of two different ego nodes can be identical except for the ego node if they have exactly the same neighbor set.
- Betweenness centrality need not be between 0 and 1 unless using a normalized definition.
- Closeness and betweenness are both based on shortest paths, but use them differently.
- On directed graphs, in-degree centrality often correlates with PageRank, but they are not the same.
- PageRank is not always the best centrality measure.
- PageRank scores sum to 1.
- A graph has one global clustering coefficient, but different community assignments can produce different modularity scores.
- Jaccard and Adamic-Adar are zero when there are no common neighbors; SimRank may be non-zero.
- Dot product is not enough for document similarity because long documents naturally share more words.
- Bag of words ignores order.
- API fields and endpoints can change; learn request/response patterns and documentation reading.

## 11. Compact Python Cheat Sheet

### Graph Loading

```python
g = nx.Graph()
with open("edges.txt", "r") as f:
    for line in f:
        u, v = line.strip().split()
        g.add_edge(int(u), int(v))
```

Weighted CSV:

```python
g = nx.Graph()
with open("sample_weighted_graph.txt", "r") as f:
    for line in f:
        u, v, w = line.strip().split(",")
        g.add_edge(u, v, weight=float(w))
```

### Basic Stats

```python
def print_graph_stats(g):
    print("Number of nodes:", g.number_of_nodes())
    print("Number of edges:", g.number_of_edges())
    print("Average node degree:", 2 * g.number_of_edges() / g.number_of_nodes())
```

### Network Properties

```python
nx.ego_graph(g, n)
nx.all_pairs_shortest_path_length(g)
nx.shortest_path_length(g, source, target)
nx.transitivity(g)
```

### Centrality

```python
nx.degree_centrality(g)
nx.closeness_centrality(g)
nx.betweenness_centrality(g)
nx.eigenvector_centrality(g)
nx.pagerank(g, alpha=0.85)
```

### Communities

```python
from networkx.algorithms.community import greedy_modularity_communities
from networkx.algorithms.community import girvan_newman
from networkx.algorithms.community import label_propagation_communities

list(greedy_modularity_communities(g))
next(girvan_newman(g))
list(label_propagation_communities(g))
```

### Node Similarity

```python
list(nx.jaccard_coefficient(g, [(u, v)]))
list(nx.adamic_adar_index(g, [(u, v)]))
sim = nx.simrank_similarity(g, importance_factor=0.85)
sim[u][v]
```

### Text Processing

```python
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
from nltk.stem.porter import PorterStemmer

stop_set = set(stopwords.words("english"))
stemmer = PorterStemmer()

def preprocess(doc):
    tokens = word_tokenize(doc.lower().replace("<br />", "\n"))
    tokens = [t for t in tokens if t and t[0].isalpha()]
    tokens = [t for t in tokens if t not in stop_set]
    tokens = [stemmer.stem(t) for t in tokens]
    return tokens
```

### Gensim BOW, LDA, Similarity

```python
from gensim import corpora, models
from gensim.similarities import SparseMatrixSimilarity

vocab = corpora.Dictionary(corpus)
vocab.filter_extremes(no_below=3, no_above=0.25)
bow = [vocab.doc2bow(doc) for doc in corpus]

index = SparseMatrixSimilarity(bow, num_terms=len(vocab), num_best=5)
index[bow[0]]

lda = models.LdaModel(bow, id2word=vocab, num_topics=10)
lda.print_topic(0, topn=5)
lda[bow[0]]
```

### VADER

```python
from nltk.sentiment.vader import SentimentIntensityAnalyzer

sia = SentimentIntensityAnalyzer()
sia.polarity_scores("This is amazing!")
```

### Word2Vec

```python
from gensim.models import Word2Vec

model = Word2Vec(docs, vector_size=50, window=5, min_count=1, negative=5, workers=4)
model.wv["word"]
model.wv.most_similar(positive=["word"], topn=10)
```

## 12. Source Files Used

- `Session 1/1 Introduction & graph fundamentals.pdf`
- `Session 1/Session 1 Summary and FAQs.pdf`
- `Session 1/Lab 1 Tutorial.ipynb`
- `Session 1/Python Review/Lab 0 Python review.pdf` as supporting Python context
- `Session 2 Social Network as Graphs/2 Social networks as graphs.pdf`
- `Session 2 Social Network as Graphs/Session 2 Summary and FAQs.pdf`
- `Session 2 Social Network as Graphs/Lab 2 Tutorial.ipynb`
- `Session 3 Social API/Lab 3 Tutorial.ipynb`
- `Session 3 Social API/YouTube API.ipynb`
- `Session 4 Social Text Analytics/4 Social text analytics.pdf`
- `Session 4 Social Text Analytics/Session 4 Summary and FAQs.pdf`
- `Session 4 Social Text Analytics/Lab 4 Tutorial.ipynb`
- `Session 5 Social Influence/5 Social influence.pdf`
- `Session 5 Social Influence/Lab 5 Tutorial.ipynb`
- `Session 6 Social Communities/6 Social communities.pdf`
- `Session 6 Social Communities/Lab 6-7 Tutorial.ipynb`
- `Session 7 Social Similarities/7 Social similarities.pdf`
- `Session 7 Social Similarities/Session 7 Summary and FAQs.pdf`
- `Session 8 Word and network embedding/8 Word and network embedding.pdf`
- `Session 8 Word and network embedding/Session 8 Summary and FAQs.pdf`
- `Session 8 Word and network embedding/Lab 8 Tutorial.ipynb`
