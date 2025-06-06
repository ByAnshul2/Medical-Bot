from functools import lru_cache
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.chat_models import ChatOpenAI
from langchain_pinecone import PineconeVectorStore
import os

@lru_cache(maxsize=1)
def get_embeddings():
    """Lazy load embeddings model"""
    return HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        model_kwargs={'device': 'cpu'}
    )

@lru_cache(maxsize=1)
def get_llm():
    """Lazy load language model"""
    return ChatOpenAI(
        openai_api_key=os.environ.get('TOGETHER_API_KEY2'),
        openai_api_base="https://api.together.xyz/v1",
        model_name="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",  # Using smaller 8B model
        temperature=0.4,
        max_tokens=500
    )

@lru_cache(maxsize=1)
def get_pinecone_store():
    """Lazy load Pinecone vector store"""
    embeddings = get_embeddings()
    return PineconeVectorStore.from_existing_index(
        index_name="medicalbot-try",
        embedding=embeddings
    )

def get_retriever(k=3):
    """Get retriever with specified number of results"""
    store = get_pinecone_store()
    return store.as_retriever(
        search_type="similarity",
        search_kwargs={"k": k}
    )

@lru_cache(maxsize=1)
def get_question_answer_chain():
    """Lazy load question answer chain"""
    from langchain.chains.combine_documents import create_stuff_documents_chain
    from langchain_core.prompts import ChatPromptTemplate
    from src.prompt import get_system_prompt
    
    llm = get_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", get_system_prompt() + "\n\nPrevious conversation context:\n{context}"),
        ("human", "{input}"),
    ])
    
    return create_stuff_documents_chain(llm, prompt) 