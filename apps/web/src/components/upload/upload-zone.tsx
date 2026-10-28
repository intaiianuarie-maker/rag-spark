"use client";

import { useCallback, useRef, useState } from "react";

import { isAcceptedMime } from "./accepted-mimes";
import { ACCEPTED_MIME_TYPES } from "./accepted-mimes";

export type UploadFileEntry = {
  file: File;
  accepted: boolean;
  reason?: string;
};

export function UploadZone({
  onFiles,
  accept = ACCEPTED_MIME_TYPES.join(","),
}: {
  onFiles: (files: File[]) => void;
  accept?: string;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      const accepted: File[] = [];
      for (const f of Array.from(files)) {
        if (isAcceptedMime(f.type)) accepted.push(f);
      }
      onFiles(accepted);
    },
    [onFiles],
  );

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 text-sm ${
        dragging ? "border-blue-500 bg-blue-50" : "border-neutral-300"
      }`}
      role="button"
      aria-label="Upload files"
    >
      <p>Drag and drop files here, or click to browse.</p>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={accept}
        className="hidden"
        onChange={(e) => handleFiles(e.target.files)}
      />
    </div>
  );
}
