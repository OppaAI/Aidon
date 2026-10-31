"""Media and Unity asset CLI commands."""

from __future__ import annotations

from pathlib import Path


def run_media_command(args, config):
    """Run a media/asset command, returning None when not applicable."""
    if args.command == "unity":
        from ..capabilities.unity import UnityAssetBuilder
        builder = UnityAssetBuilder(config.home / "unity")
        if args.action == "asset":
            result = builder.generate_asset(args.name, shape=args.shape, color=args.color)
            print(f"Unity asset: {result['name']}, shape {result['shape']}")
            print(f"OBJ: {result['obj']}")
            print(f"C# script: {result['script']}")
            print(f"Import: open the folder in Unity, the OBJ imports as a mesh, the C# script is a MonoBehaviour that colours it {args.color}.")
            print(f"Build command, if Unity is installed: {builder.build_command(result['project_dir'])}")
            return 0
        print(builder.build_command(config.home / "unity" / args.name))
        return 0
    if args.command == "image":
        from ..capabilities.image_gen import generate_image, edit_image, save_image
        from pathlib import Path as _Path
        if not config.api_key or not config.api_base:
            print("Image generation needs AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark, no separate image key.")
            return 2
        out = _Path(args.output) if args.output else config.home / "library" / "files" / "image.png"
        if args.action == "generate":
            data = generate_image(args.prompt, config.api_key, config.api_base, size=args.size, model=config.image_model)
        else:
            data = edit_image(args.prompt, config.api_key, config.api_base, reference_images=args.refs or [], size=args.size, model=config.image_model)
        path = save_image(data, out)
        print(f"Image saved: {path}, model {config.image_model}")
        return 0
    if args.command == "video":
        from ..capabilities.video import edit_video, extract_frames, encode_frames, ffmpeg_available
        if not ffmpeg_available():
            print("ffmpeg not found on PATH, install ffmpeg to run the video pipeline, no fake video is produced.")
            return 2
        if args.action == "extract":
            frames = extract_frames(args.input, args.output, fps=args.fps)
            print(f"Extracted {len(frames)} frames to {args.output}")
            return 0
        if args.action == "encode":
            from pathlib import Path as _P
            frames = sorted(_P(args.input).glob("frame*.jpg"))
            out = encode_frames(frames, args.output, fps=args.fps)
            print(f"Encoded video: {out}")
            return 0
        if not args.prompt:
            print("video edit needs --prompt, a noun phrase for SAM, for example --prompt 'red toolbox'")
            return 2
        if not config.api_key or not config.api_base:
            print("video edit needs SAM via AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark.")
            return 2
        from ..capabilities.sam import SAMClient
        sam_client = SAMClient(config.api_base, config.api_key, config.sam_model)
        result = edit_video(args.input, args.output, args.prompt, sam_client, fps=args.fps)
        if "error" in result:
            print(f"Video pipeline error: {result['error']}")
            return 1
        print(f"Video edited: {result['output']}, frames: {len(result['frames'])}, prompt: {result['prompt']}")
        print("Edited video uses SAM box overlays, not pixel-mask compositing, mask decoding needs the optional meta-sam-parser package, see src/video.py.")
        return 0
    if args.command == "sam":
        if not config.api_key or not config.api_base:
            print("SAM needs AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark.")
            return 2
        from ..capabilities.sam import SAMClient, build_object_prompt
        client_sam = SAMClient(config.api_base, config.api_key, config.sam_model)
        result = client_sam.segment(args.images, build_object_prompt(args.prompt), propagation=args.propagation)
        if "error" in result:
            print(f"SAM error: {result['error']}")
            return 1
        for seg in result["segments"]:
            print(f"frame {seg['frame']} id {seg['object_id']} box {seg['box']} confidence {seg['confidence']}")
        print("SAM is the segment and track step only. Frame extraction and video re-encoding need ffmpeg, not built here.")
        return 0
    return None
