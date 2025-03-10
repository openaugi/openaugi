# atomic_extractor.py
import json
import re
from dotenv import load_dotenv
from typing import List, Dict, Any
from llama_index.core.schema import Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.llms.openai import OpenAI
from interfaces import AtomicExtractor
from config import DOT_ENV_PATH


class SimpleLLMAtomicExtractor(AtomicExtractor):
    def __init__(self, llm_model: str = "gpt-4o-mini-2024-07-18"):
        """
        Initialize the extractor with the specified LLM model.

        Args:
            llm_model: The model to use for extraction.
        """
        load_dotenv(DOT_ENV_PATH)  # Load environment variables
        self.llm = OpenAI(model=llm_model, temperature=0.1)
        self.text_splitter = SentenceSplitter(chunk_size=1024, chunk_overlap=100)

    def clean_llm_json_response(self, response_text: str):
        # Pattern to match JSON content between triple backticks
        pattern = r"```(?:json)?\s*([\s\S]*?)```"

        # Try to find the pattern
        match = re.search(pattern, response_text)

        if match:
            # Extract the JSON content
            json_str = match.group(1)
            return json.loads(json_str)
        else:
            # If no backticks, try parsing the whole thing
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                # If that fails, raise an error
                raise ValueError("Could not extract valid JSON from the response")

    def split_into_sections(self, text: str) -> List[str]:
        """
        Split document into manageable sections.

        Args:
            text: The full document text

        Returns:
            List of text sections
        """
        # First attempt to detect natural sections (headings, paragraphs, etc.)
        section_markers = [
            r"\n+\s*#+\s+",  # Markdown headings
            r"\n+\s*Section\s+\d+",  # Explicit section labels
            r"\n+\s*Topic\s*:|\n+\s*Subject\s*:",  # Topic markers
            r"\n{2,}",  # Multiple newlines (paragraph breaks)
        ]

        for marker in section_markers:
            sections = re.split(marker, text)
            if len(sections) > 1:
                # Found natural sections, clean them up
                return [s.strip() for s in sections if s.strip()]

        # If no natural sections are found, fall back to chunking
        chunks = self.text_splitter.split_text(text)
        return chunks

    def generate_global_summary(self, document: Document) -> str:
        """
        Generate a global summary of the document.

        Args:
            document: The Document object

        Returns:
            Summary text
        """
        prompt = """
        Create a concise summary that captures:
        1. The main topics discussed in this document
        2. Key points for each topic
        3. Any important relationships between concepts

        Keep the summary focused and highlight only the most important information.

        DOCUMENT:
        {text}

        SUMMARY:
        """

        response = self.llm.complete(prompt.format(text=document.text))
        return response.text

    def extract_section_ideas(
        self, section: str, global_summary: str
    ) -> List[Dict[str, Any]]:
        """
        Extract atomic ideas from a section, with reference to the global summary.

        Args:
            section: The section text
            global_summary: The global summary for context

        Returns:
            List of atomic ideas with metadata
        """
        standard_prompt = """
        You are extracting distinct atomic ideas from a section of text.

        Here is the global context of the entire document:
        {global_summary}

        Now analyze this specific section and extract 3-5 distinct atomic ideas.
        For each idea:
        1. Provide a concise title (5-7 words)
        2. Provide a clear description (1-2 sentences)
        3. Identify any related ideas mentioned in the global summary

        Format your response as a JSON list:
        [
          {{
            "title": "Idea title",
            "description": "Idea description",
            "links": ["related idea title", "related idea title"]
          }}
        ]

        SECTION TEXT:
        {section}

        ATOMIC IDEAS (JSON format):
        """

        response = self.llm.complete(
            standard_prompt.format(global_summary=global_summary, section=section)
        )

        try:
            # Try to parse the JSON response
            ideas = self.clean_llm_json_response(response.text)
            if ideas and len(ideas) > 0:
                return ideas
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON response in both attempts: {e}")
            # Return a minimal valid structure
            return [
                {
                    "title": "Section content",
                    "description": section[:100] + "...",
                    "links": [],
                }
            ]

    def deduplicate_ideas(
        self, all_ideas: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Deduplicate similar ideas across sections.

        Args:
            all_ideas: All extracted ideas

        Returns:
            Deduplicated list of ideas
        """
        if not all_ideas:
            return []

        # Prepare all ideas as context
        ideas_context = "\n".join(
            [
                f"IDEA {i+1}: {idea['title']} - {idea['description']}"
                for i, idea in enumerate(all_ideas)
            ]
        )

        prompt = """
        You are deduplicating similar ideas extracted from a document.

        Here are all the extracted ideas:
        {ideas_context}

        For each idea, determine if it is a duplicate or very similar to another idea.
        Group similar ideas and provide a merged representation that captures all important details.

        Return your answer as a JSON list of deduplicated ideas:

        [
          {{
            "title": "Merged idea title",
            "description": "Merged idea description",
            "links": ["related concept 1", "related concept 2"],
            "source_ideas": [original idea indices that were merged]
          }}
        ]

        Ensure you return valid JSON.
        """

        response = self.llm.complete(prompt.format(ideas_context=ideas_context))

        try:
            return self.clean_llm_json_response(response.text)
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON response in deduplication: {str(e)}")
            print(f"{response.text}")
            return all_ideas  # Return original ideas if deduplication fails

    def extract_atomic_ideas(self, documents: List[Document], store=None) -> List[Document]:
        """
        Extract atomic ideas from documents, linking back to source docs.
        Skip documents that have already been processed if store is provided.

        Args:
            documents: List of source Documents
            store: Optional AtomicIdeaStore to check for already processed documents

        Returns:
            List of new Documents, each representing an atomic idea
        """
        all_atomic_documents = []
        documents_to_process = []

        # First filter out already processed documents if store is provided
        if store:
            for doc in documents:
                if not store.has_processed_document(doc.doc_id):
                    documents_to_process.append(doc)

            if len(documents_to_process) < len(documents):
                print(f"Skipping {len(documents) - len(documents_to_process)} already processed documents")
        else:
            documents_to_process = documents

        # If no documents need processing, return empty list
        if not documents_to_process:
            print("No new documents to process")
            return []

        print(f"Processing {len(documents_to_process)} documents")

        for doc in documents_to_process:
            # Generate a global summary
            global_summary = self.generate_global_summary(doc)

            # Split into sections
            sections = self.split_into_sections(doc.text)

            # Extract ideas from each section
            all_ideas = []
            for section in sections:
                section_ideas = self.extract_section_ideas(section, global_summary) or []
                all_ideas.extend(section_ideas)

            # Deduplicate ideas
            deduplicated_ideas = self.deduplicate_ideas(all_ideas)

            # Convert each idea to a Document
            for idea in deduplicated_ideas:
                # Create metadata that links back to the source document
                metadata = {
                    "source_doc_id": doc.doc_id,
                    "source_doc_title": doc.metadata.get("title", ""),
                    "source_doc_path": doc.metadata.get("file_path", ""),
                    "idea_title": idea["title"],
                    "links": idea.get("links", []),
                    "is_atomic_idea": True
                }

                # Create a new Document for this atomic idea
                atomic_doc = Document(
                    text=idea["description"],
                    metadata=metadata
                )

                all_atomic_documents.append(atomic_doc)

        print(f"Extracted {len(all_atomic_documents)} atomic ideas from {len(documents_to_process)} documents")
        return all_atomic_documents