from io import BytesIO
from pathlib import Path

from PIL import Image, ImageSequence


MAX_OCR_PAGES = 40
MAX_IMAGE_PIXELS = 40_000_000
OCR_TEXT_THRESHOLD = 40
SUPPORTED_IMAGE_EXTENSIONS = {
    ".apng", ".avif", ".blp", ".bmp", ".dib", ".cur", ".dds", ".gif",
    ".icns", ".ico", ".j2c", ".j2k", ".jfif", ".jp2", ".jpc", ".jpe",
    ".jpeg", ".jpf", ".jpx", ".jpg", ".msp", ".pbm", ".pcx", ".pgm",
    ".png", ".pnm", ".ppm", ".psd", ".qoi", ".ras", ".sgi", ".tga",
    ".tif", ".tiff", ".webp", ".xbm", ".xpm",
}


def _ocr_pdf_pages(data, extracted_pages):
    import pypdfium2
    import pytesseract

    with pypdfium2.PdfDocument(data) as pdf:
        if len(pdf) > MAX_OCR_PAGES:
            raise ValueError(
                f"Scanned PDF has more than {MAX_OCR_PAGES} pages; split it into smaller files."
            )
        for page_number in range(len(pdf)):
            if len(extracted_pages[page_number].strip()) >= OCR_TEXT_THRESHOLD:
                continue
            page = pdf[page_number]
            width, height = page.get_size()
            scale = 200 / 72
            if width * scale * height * scale > MAX_IMAGE_PIXELS:
                raise ValueError("A scanned PDF page is too large to OCR safely.")
            with page.render(scale=scale).to_pil() as image:
                ocr_text = pytesseract.image_to_string(image)
            extracted_pages[page_number] = "\n".join(
                text for text in (extracted_pages[page_number], ocr_text) if text.strip()
            )
    return "\n".join(extracted_pages)


def extract_file(filename, data):
    extension = Path(filename).suffix.lower()
    if extension == ".pdf":
        from pypdf import PdfReader

        pages = [
            page.extract_text() or ""
            for page in PdfReader(BytesIO(data)).pages
        ]
        if all(len(page.strip()) >= OCR_TEXT_THRESHOLD for page in pages):
            return "\n".join(pages)
        return _ocr_pdf_pages(data, pages)

    if extension in {".txt", ".md", ".csv", ".json", ".xml", ".html"}:
        return data.decode("utf-8", errors="replace")

    if extension in SUPPORTED_IMAGE_EXTENSIONS:
        import pytesseract

        with Image.open(BytesIO(data)) as image:
            frame_count = getattr(image, "n_frames", 1)
            if frame_count > MAX_OCR_PAGES:
                raise ValueError(
                    f"Image has more than {MAX_OCR_PAGES} frames/pages; split it into smaller files."
                )
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image is too large to OCR safely.")
            text = []
            for frame in ImageSequence.Iterator(image):
                with frame.convert("RGB") as converted:
                    text.append(pytesseract.image_to_string(converted))
        return "\n".join(text)

    raise ValueError("Unsupported file type")
