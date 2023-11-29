import os
import toml
import copy
import shutil
import logging
import asyncio
import argparse
from datetime import datetime

from waifuc.action import PersonSplitAction
from waifuc.action import MinSizeFilterAction
from waifuc.action import ThreeStageSplitAction

from anime2sd import extract_and_remove_similar
from anime2sd import classify_from_directory
from anime2sd import select_dataset_images_from_directory
from anime2sd import tag_and_caption_from_directory
from anime2sd import arrange_folder, get_repeat
from anime2sd import read_weight_mapping
from anime2sd import CharacterTagProcessor, TaggingManager, CaptionGenerator

from anime2sd.basics import (
    rearrange_related_files,
    load_metadata_from_aux,
    setup_logging,
)
from anime2sd.parse_arguments import parse_arguments
from anime2sd.waifuc_customize import LocalSource, SaveExporter
from anime2sd.waifuc_customize import MinFaceCountAction, MinHeadCountAction


def update_args_from_toml(
    args: argparse.Namespace, toml_path: str
) -> argparse.Namespace:
    """
    Update a copy of args with configurations from a TOML file.

    This function reads a TOML file and updates the attributes of the given
    argparse.Namespace object with the configurations found in the file.
    If the TOML file contains nested sections, they are flattened.

    Args:
        args (argparse.Namespace):
            The original argparse Namespace object containing command-line arguments.
        toml_path (str):
            Path to the TOML configuration file.

    Returns:
        argparse.Namespace:
            A new Namespace object with updated configurations from the TOML file.

    Raises:
        Exception: If there is an error in reading or parsing the TOML file.
    """
    new_args = copy.deepcopy(args)
    try:
        with open(toml_path, "r") as f:
            config = toml.load(f)
        for key, value in config.items():
            if isinstance(value, dict):
                # Handle nested sections by flattening them
                for nested_key, nested_value in value.items():
                    setattr(new_args, nested_key, nested_value)
            else:
                setattr(new_args, key, value)
    except Exception as e:
        print(f"Error loading config from {toml_path}: {e}")
    return new_args


def setup_args(args):
    """
    Sets up the start and end stages for the pipeline based on the provided arguments.
    If the 'image_type' is not specified, it defaults to the 'pipeline_type'.
    """
    # Mapping stage numbers to their aliases
    STAGE_ALIASES = {
        1: ["extract"],
        2: ["crop"],
        3: ["classify"],
        4: ["select"],
        5: ["tag", "caption", "tag_and_caption"],
        6: ["arrange"],
        7: ["balance"],
    }
    if not config.image_type:
        config.image_type = args.pipeline_type

    start_stage = args.start_stage
    end_stage = args.end_stage

    # Convert stage aliases to numbers if provided
    for stage_number in STAGE_ALIASES:
        if args.start_stage in STAGE_ALIASES[stage_number]:
            start_stage = stage_number
        if args.end_stage in STAGE_ALIASES[stage_number]:
            end_stage = stage_number

    args.start_stage = int(start_stage)
    args.end_stage = int(end_stage)


def get_and_create_dst_dir(
    args: argparse.Namespace,
    mode: str,
    sub_dir: str = "",
    makedirs: bool = True,
) -> str:
    """
    Constructs the destination directory path based on the mode, subdirectory,
    and additional arguments.

    If 'makedirs' is True, the function also creates the directory if it doesn't exist.

    Args:
        args (argparse.Namespace):
            The namespace object containing the command-line arguments.
        mode (str):
            The mode specifying the main directory under the destination directory.
        sub_dir (str, optional):
            An additional subdirectory to put at the end.
            Defaults to an empty string.
        makedirs (bool, optional):
            Whether to create the directory if it doesn't exist. Defaults to True.

    Returns:
        str: The path to the constructed destination directory.
    """
    dst_dir = os.path.join(
        args.dst_dir, mode, args.extra_path_component, args.image_type, sub_dir
    )
    if makedirs:
        os.makedirs(dst_dir, exist_ok=True)
    return dst_dir


def get_src_dir(args, stage):
    """
    Determines the source directory for a given stage of the pipeline.

    Args:
        args (argparse.Namespace):
            The namespace object containing the command-line arguments.
        stage (int): The current stage of the pipeline.

    Returns:
        str: The path to the source directory for the given stage.

    Raises:
        ValueError: If the provided stage number is invalid.
    """
    if stage == args.start_stage or stage == 1:
        return args.src_dir
    elif stage == 2:
        return get_and_create_dst_dir(args, "intermediate", "raw", makedirs=False)
    elif stage == 3:
        return get_and_create_dst_dir(args, "intermediate", "cropped", makedirs=False)
    elif stage == 4:
        return get_and_create_dst_dir(args, "intermediate", makedirs=False)
    elif stage == 5:
        return get_and_create_dst_dir(args, "training", makedirs=False)
    elif stage == 6:
        dst_dir = get_src_dir(args, 5)
        for _ in range(args.rearrange_up_levels):
            dst_dir = os.path.dirname(dst_dir)
        return dst_dir
    elif stage == 7:
        dst_dir = get_src_dir(args, 6)
        for _ in range(args.compute_multiply_up_levels):
            dst_dir = os.path.dirname(dst_dir)
        return dst_dir
    else:
        raise ValueError(f"Invalid stage: {stage}")


def extract_frames(args, stage, logger):
    """
    Extracts frames from videos and saves them to the destination directory.
    This function also handles duplicate detection and removal.
    """
    # Get the path to the source directory containing the videos
    src_dir = get_src_dir(args, stage)
    # Get the path to the destination directory for the extracted frames
    dst_dir = get_and_create_dst_dir(args, "intermediate", "raw")
    logger.info(f"Extracting frames to {dst_dir} ...")

    extract_and_remove_similar(
        src_dir,
        dst_dir,
        args.image_prefix,
        ep_init=args.ep_init,
        extract_key=args.extract_key,
        model_name=args.detect_duplicate_model,
        thresh=args.similar_thresh,
        to_remove_similar=not args.no_remove_similar,
        logger=logger,
    )


# TODO: Avoid cropping for already cropped data
def crop_characters(args, stage, logger):
    """Crops individual characters from images in the source directory."""
    # Get the path to the source directory containing the images to crop from
    src_dir = get_src_dir(args, stage)
    # Get the path to the destination directory for the cropped images
    dst_dir = get_and_create_dst_dir(args, "intermediate", "cropped")
    logger.info(f"Cropping individual characters to {dst_dir} ...")

    overwrite_path = args.start_stage == stage and args.overwrite_path

    source = LocalSource(src_dir, overwrite_path=overwrite_path)
    detect_config_person = {"level": args.detect_level}
    if args.detect_level in ["s", "n"]:
        detect_level_head_halfbody = args.detect_level
    else:
        detect_level_head_halfbody = "n"
    detect_config = {"level": detect_level_head_halfbody}
    crop_action = (
        ThreeStageSplitAction(
            split_person=True,
            head_conf=detect_config,
            halfbody_conf=detect_config,
            person_conf=detect_config_person,
        )
        if args.use_3stage_crop == 2
        else PersonSplitAction(keep_original=False, level=args.detect_level)
    )

    source = source.attach(
        # NoMonochromeAction(),
        crop_action,
        MinSizeFilterAction(args.min_crop_size),
        # Not used here because it can be problematic for multi-character scene
        # Some not moving while other moving
        # FilterSimilarAction('all'),
    )
    if args.crop_with_head:
        source = source.attach(
            MinHeadCountAction(1, level="n"),
        )
    if args.crop_with_face:
        source = source.attach(
            MinFaceCountAction(1, level="n"),
        )

    source.export(SaveExporter(dst_dir, no_meta=False, save_caption=False))


def classify_characters(args, stage, logger):
    """Classifies characters in the given source directory."""

    # Get the path to the source directory containing images to be classified
    src_dir = get_src_dir(args, stage)
    # Get the path to the distination directory containing the classified images
    dst_dir = get_and_create_dst_dir(args, "intermediate", "classified")

    # Determine whether to move or copy files to the destination directory.
    move = args.remove_intermediate or (src_dir == dst_dir)
    # Determine whether to ignore existing character metadata.
    ignore_character_metadata = (
        args.ignore_character_metadata or args.pipeline_type == "screenshots"
    )

    # Log information about the classification process.
    logger.info(f"Classifying characters to {dst_dir} ...")

    # Call the `classify_from_directory` function with the specified parameters.
    classify_from_directory(
        src_dir,
        dst_dir,
        ref_dir=args.character_ref_dir,
        ignore_character_metadata=ignore_character_metadata,
        to_extract_from_noise=not args.no_extract_from_noise,
        to_filter=not args.no_filter_characters,
        keep_unnamed=args.keep_unnamed_clusters,
        clu_min_samples=args.cluster_min_samples,
        merge_threshold=args.cluster_merge_threshold,
        same_threshold_rel=args.same_threshold_rel,
        same_threshold_abs=args.same_threshold_abs,
        move=move,
        logger=logger,
    )


def select_dataset_images(args, stage, logger):
    """Construct training set from classified images and raw images."""
    # Get the path to the intermediate directory containing the
    # two folders "raw" and "classified".
    src_dir = get_src_dir(args, stage)
    classified_dir = os.path.join(src_dir, "classified")
    full_dir = os.path.join(src_dir, "raw")
    # Get the path to the image_type subfolder of the training directory
    dst_dir = get_and_create_dst_dir(args, "training")

    is_start_stage = args.start_stage == stage
    if is_start_stage:
        # rearrange json and ccip in case of manual inspection
        rearrange_related_files(classified_dir, logger)
    overwrite_path = is_start_stage and args.overwrite_path

    logger.info(f"Preparing dataset images to {dst_dir} ...")

    select_dataset_images_from_directory(
        classified_dir,
        full_dir,
        dst_dir,
        pipeline_type=args.pipeline_type,
        overwrite_path=overwrite_path,
        # For saving character to metadata
        character_overwrite_uncropped=args.character_overwrite_uncropped,
        character_remove_unclassified=args.character_remove_unclassified,
        # For saving embedding initialization information
        image_type=args.image_type,
        overwrite_emb_init_info=args.overwrite_emb_init_info,
        # For 3 stage cropping
        use_3stage_crop=args.use_3stage_crop == 4,
        detect_level=args.detect_level,
        # For resizing/copying images to destination
        max_size=args.max_size,
        image_save_ext=args.image_save_ext,
        to_resize=not args.no_resize,
        n_anime_reg=args.n_anime_reg,
        # For additional filtering after obtaining dataset images
        filter_again=args.filter_again,
        detect_duplicate_model=args.detect_duplicate_model,
        similarity_threshold=args.similar_thresh,
        logger=logger,
    )

    if args.remove_intermediate:
        shutil.rmtree(classified_dir)


def tag_and_caption(args, stage, logger):
    """Perform in-place tagging and captioning."""
    # Get path to the directiry containing images to be tagged and captioned
    src_dir = get_src_dir(args, stage)
    if args.start_stage == stage:
        # rearrange json and ccip in case of manual inspection
        rearrange_related_files(src_dir, logger)

    if "character" in args.pruned_mode:
        char_tag_proc = CharacterTagProcessor(
            tag_list_path=args.character_tags_file,
            drop_difficulty=args.drop_difficulty,
            emb_min_difficulty=args.emb_min_difficulty,
            emb_max_difficutly=args.emb_max_difficulty,
            drop_all=args.drop_all_core,
            emb_init_all=args.emb_init_all_core,
        )
    else:
        char_tag_proc = None

    tagging_manager = TaggingManager(
        tagging_method=args.tagging_method,
        tag_threshold=args.tag_threshold,
        overwrite_tags=args.overwrite_tags,
        pruned_mode=args.pruned_mode,
        blacklist_tags_file=args.blacklist_tags_file,
        overlap_tags_file=args.overlap_tags_file,
        character_tag_processor=char_tag_proc,
        process_from_original_tags=args.process_from_original_tags,
        sort_mode=args.sort_mode,
        max_tag_number=args.max_tag_number,
        logger=logger,
    )

    caption_generator = CaptionGenerator(
        character_sep=args.character_sep,
        character_inner_sep=args.character_inner_sep,
        character_outer_sep=args.character_outer_sep,
        caption_inner_sep=args.caption_inner_sep,
        caption_outer_sep=args.caption_outer_sep,
        use_npeople_prob=args.use_npeople_prob,
        use_character_prob=args.use_character_prob,
        use_copyright_prob=args.use_copyright_prob,
        use_image_type_prob=args.use_image_type_prob,
        use_artist_prob=args.use_artist_prob,
        use_rating_prob=args.use_rating_prob,
        use_tags_prob=args.use_tags_prob,
    )

    logger.info(f"Tagging and captioning images in {src_dir} ...")

    tag_and_caption_from_directory(
        src_dir,
        tagging_manager,
        caption_generator,
        # For core tags
        use_existing_core_tag_file=args.use_existing_core_tag_file,
        core_frequency_threshold=args.core_frequency_thresh,
        # For saving embedding initialization information
        image_type=args.image_type,
        overwrite_emb_init_info=args.overwrite_emb_init_info,
        # For file io
        load_aux=args.load_aux,
        save_aux=args.save_aux,
        overwrite_path=args.overwrite_path,
        logger=logger,
    )


def rearrange(args, stage, logger):
    """Rearrange the images in the directory."""
    # Get path to the directiry containing images to be rearranged
    src_dir = get_src_dir(args, stage)
    logger.info(f"Rearranging {src_dir} ...")
    if args.start_stage == stage and args.load_aux:
        load_metadata_from_aux(
            src_dir, args.load_aux, args.save_aux, args.overwrite_path, logger=logger
        )
        rearrange_related_files(src_dir, logger)
    arrange_folder(
        src_dir,
        src_dir,
        args.arrange_format,
        args.max_character_number,
        args.min_images_per_combination,
        logger=logger,
    )


def balance(args, stage, logger):
    """Compute the repeat for the images in the directory."""
    # Get path to the directiry containing images for which repeat needs to be computed
    src_dir = get_src_dir(args, stage)
    if args.start_stage == stage and args.load_aux:
        load_metadata_from_aux(
            src_dir, args.load_aux, args.save_aux, args.overwrite_path, logger=logger
        )
        rearrange_related_files(src_dir, logger)
    logger.info(f"Computing repeat for {src_dir} ...")
    if args.weight_csv is not None:
        weight_mapping = read_weight_mapping(args.weight_csv)
    else:
        weight_mapping = None
    current_time = datetime.now()
    str_current_time = current_time.strftime("%Y-%m-%d%H-%M-%S")
    if args.log_dir.lower() == "none":
        log_file = None
    else:
        log_file = os.path.join(
            args.log_dir, f"{args.log_prefix}_weighting_{str_current_time}.log"
        )
    get_repeat(
        src_dir,
        weight_mapping,
        args.min_multiply,
        args.max_multiply,
        log_file,
        logger=logger,
    )


def identify_dependencies(configs):
    stage3_dependencies = {}
    for i, config in enumerate(configs):
        if config.pipeline_type != "booru" or config.n_add_to_ref_per_character <= 0:
            dependent_configs = [
                j
                for j, dep_config in enumerate(configs)
                if dep_config.character_ref_dir == config.character_ref_dir
                and dep_config.pipeline_type == "booru"
                and dep_config.n_add_to_ref_per_character > 0
            ]
            if dependent_configs:
                stage3_dependencies[i] = dependent_configs
    return stage3_dependencies


async def run_stage(config, config_index, stage_num, stage_events, logger):
    # Mapping stage numbers to their respective function names
    STAGE_FUNCTIONS = {
        1: extract_frames,
        2: crop_characters,
        3: classify_characters,
        4: select_dataset_images,
        5: tag_and_caption,
        6: rearrange,
        7: balance,
    }
    STAGE_FUNCTIONS[stage_num](config, stage_num, logger)
    stage_events[config_index][stage_num].set()


async def run_pipeline(config, config_index, stage_events, stage3_dependencies):
    logger = setup_logging(
        config.log_dir,
        f"{config.pipeline_type}_{config.log_prefix}",
        f"pipeline_{config_index}",
    )
    config = configs[config_index]
    # Loop through the stages and execute them
    for stage_num in range(config.start_stage, config.end_stage + 1):
        if stage_num == 3 and config_index in stage3_dependencies:
            # Wait for dependent booru configs to complete stage 3
            await asyncio.gather(
                *(
                    stage_events[dep_index][3].wait()
                    for dep_index in stage3_dependencies[config_index]
                )
            )

        logger.info(f"-------------Start stage {stage_num}-------------")
        await run_stage(config, config_index, stage_num, stage_events, logger)


async def main(configs):
    stage3_dependencies = identify_dependencies(configs)

    # Initialize events for each stage of each config
    stage_events = [
        {j: asyncio.Event() for j in range(config.start_stage, config.end_stage + 1)}
        for config in configs
    ]

    # Run pipelines asynchronously with dependencies
    await asyncio.gather(
        *(
            run_pipeline(config, i, stage_events, stage3_dependencies)
            for i, config in enumerate(configs)
        )
    )


if __name__ == "__main__":
    args, explicit_args = parse_arguments()

    if args.base_config_file:
        args = update_args_from_toml(args, args.base_config_file)

    configs = []
    if args.config_file:
        for toml_path in args.config_file:
            config_args = update_args_from_toml(args, toml_path)
            configs.append(config_args)
    else:
        configs.append(args)

    if args.base_config_file or args.config_file:
        # Overwrite args with explicitly set command line arguments
        for config in configs:
            for key, value in explicit_args.items():
                setattr(config, key, value)

    # A set to record dst_dir and image_type in configs
    dst_folder_set = set()

    # Process each configuration
    for config in configs:
        setup_args(config)
        dst_folder = (config.dst_dir, config.extra_path_component, config.image_type)
        if dst_folder in dst_folder_set:
            raise ValueError(
                "Duplicate (dst_dir, extra_path_component, image_type) "
                "is not supported: "
                f"{config.dst_dir}, {config.extra_path_component}, {config.image_type}"
            )
        dst_folder_set.add(dst_folder)

    logging.getLogger().setLevel(logging.INFO)
    asyncio.run(main(configs))
