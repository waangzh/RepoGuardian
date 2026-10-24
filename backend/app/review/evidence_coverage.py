"""证据请求是否已被现有目录覆盖的确定性判断。"""


def request_covered(request, catalog):
    cursor = request.start_line
    for start, end in sorted((value["start_line"], value["end_line"]) for value in catalog.values()
                             if value["file_path"] == request.file_path and value.get("side", "head") == "head"):
        if start > cursor:
            break
        cursor = max(cursor, end + 1)
    return cursor > request.end_line
